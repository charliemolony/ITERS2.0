from highway_env import utils
import random
import numpy as np
import copy
from highway_env.envs import highway_env
from highway_env.vehicle.controller import ControlledVehicle
import torch
from src.feedback.feedback_processing import encode_trajectory


class CustomHighwayEnv(highway_env.HighwayEnvFast):

    def __init__(self, shaping=False, time_window=5,run_tailgaiting=False):
        super().__init__()
        self.shaping = shaping
        self.time_window = time_window
        self.run_tailgaiting=run_tailgaiting

        self.config["tailgating"] = {
            # "thresholds": {
            #     "thresholdX": 6,
            #     "thresholdY": 1.7
            # },
            "reward": 0
            ,
            "observation": {
                "type": "Kinematics",
                "absolute": False 
            }}
        

        self.episode = []
        self.number_of_features=5

        if self.run_tailgaiting:
            self.number_of_agents=5
            self.ego_index=0

        else:
            self.number_of_agents =1
        self.state_len = self.number_of_features*self.number_of_agents

        self.lows = np.zeros((self.state_len, ))
        self.highs = np.ones((self.state_len, ))

        # speed is in [-1, 1]
        self.lows[[3, 4]] = -1
        self.action_dtype = 'int'
        self.state_dtype = 'cont'

        self.lane = 0
        self.lmbda = 0.2
        self.epsilon=0
        self.lane_changed = []
        self.thresholdX=0.04
        self.thresholdY=0.15


        self.tailgating_traj=[]


        # presence features are immutable
        self.immutable_features = [0]

        self.discrete_features = [0]
        self.cont_features = [f for f in range(self.state_len) if f not in self.discrete_features]

        self.max_changed_lanes = 3

    def step(self, action):
        self.episode.append((self.state, action))

        curr_lane = self.vehicle.target_lane_index[2] if isinstance(self.vehicle, ControlledVehicle) else self.vehicle.lane_index[2]
        self.state, rew, done, info = super().step(action) 
                                                        
        info['true_rew'] = rew

        neighbours = self.road.network.all_side_lanes(self.vehicle.lane_index)
        self.lane = self.vehicle.target_lane_index[2] if isinstance(self.vehicle, ControlledVehicle) else self.vehicle.lane_index[2]

        coll_rew = 0
        if self.vehicle.crashed: 
            coll_rew = self.config["collision_reward"] 

            

        tailgating_flag=False
        if self.run_tailgaiting:
            for location in self.state[1:]: 
                other_vehicle_x, other_vehicle_y=abs(location[1]),abs(location[2])
                if other_vehicle_x<=self.thresholdX and other_vehicle_y<=self.thresholdY: ## check if the agent has gotten to close to any vehicles
                    tailgating_flag=True


        tailgating_rew=tailgating_flag*self.config['tailgating']['reward']
   
        self.lane_changed.append(self.lane != curr_lane)

        forward_speed = self.vehicle.speed * np.cos(self.vehicle.heading)
        scaled_speed = utils.lmap(forward_speed, self.config["reward_speed_range"], [0, 1])
        speed_rew = self.config["high_speed_reward"] * np.clip(scaled_speed, 0, 1)

        
        rightmost_lane_index = max(len(neighbours) - 1, 0)
        is_in_rightmost_lane = int(self.lane == rightmost_lane_index)
        right_lane_rew = self.config["right_lane_reward"] * is_in_rightmost_lane


        lane_change = sum(self.lane_changed[-self.time_window:]) >= self.max_changed_lanes
        true_reward = self.calculate_true_reward(rew, lane_change,tailgating_flag,neighbours)

        aug_rew = 0
        if self.shaping:
            aug_rew = self.augment_reward(action, self.state)

        rew += aug_rew
        lane_change_weight=self.config['lane_change_reward']
        add_lane_change=lane_change * lane_change_weight
        rew += add_lane_change

        if self.run_tailgaiting:
            rew += tailgating_rew    
        
            info['rewards'] = {'collision_rew': coll_rew,
                             'right_lane': float(is_in_rightmost_lane),  
                            'right_lane_rew': right_lane_rew,
                            'speed_rew': speed_rew,
                            'tailgating_rew': tailgating_rew,
                            'tailgating': float(tailgating_flag),
                            'lane_change_rew': aug_rew,   
                            'lane_changed': lane_change,
                            'true_reward': true_reward}
        else: 
            info['rewards'] = {'collision_rew': coll_rew,
                'right_lane_rew': right_lane_rew,
                'speed_rew': speed_rew,
                'lane_change_rew': aug_rew,   
                'lane_changed': lane_change,
                'true_reward': true_reward}

        return self.state, rew, done, info

    def calculate_true_reward(self, rew, lane_change,tailgating_flag,neighbours):
        if self.run_tailgaiting:
            tailgating_rew=tailgating_flag*self.true_rewards['tailgating']['reward']
            rightmost_lane_index = max(len(neighbours) - 1, 0)
            is_in_rightmost_lane = int(self.lane == rightmost_lane_index)
            right_lane_rew = self.true_rewards["right_lane_reward"] * is_in_rightmost_lane
            return rew + tailgating_rew + self.true_rewards['lane_change_reward'] * lane_change +right_lane_rew 

        return rew + self.true_rewards['lane_change_reward'] * lane_change 

    def reset(self):
        self.episode = []
        self.lane_changed = []
        self.state= super().reset()

        self.lane = self.vehicle.target_lane_index[2] if isinstance(self.vehicle, ControlledVehicle) else self.vehicle.lane_index[2]

        return self.state

    def close(self):
        pass

    def render(self):
        super().render()

    def render_state(self, state):
        print('State = {}'.format(state.flatten()[0:5]))

    def augment_reward(self, action, state):
        running_rew = 0
        past = copy.copy(self.episode)
        curr = 1

        for j in range(len(past)-1, -1, -1):  # go backwards in the past
            state_enc = encode_trajectory(past[j:], state, curr, self.time_window, self)

            rew = self.reward_model.predict(state_enc)
            running_rew += self.lmbda * rew.item()

            if curr >= self.time_window:
                break

            curr += 1

        return running_rew

    def set_reward_model(self, rm):
        self.reward_model = rm

    def set_shaping(self, boolean):
        self.shaping = boolean

    def set_true_reward(self, rewards):
        self.true_rewards = rewards

    def random_state(self):
        return np.random.uniform(self.lows, self.highs, (self.state_len, ))

    def encode_state(self, state):
        if self.run_tailgaiting:
            encoded_state=state.flatten() ## for tailgaiting you need the state of every vehicle, not just the ego
        else:
            encoded_state=state[0].flatten() ### original 
        return encoded_state
          
    def important_feature_indices(self,rule):
        return     [index + 1 for index, (feature, details) in enumerate(rule['features'].items()) if details.get('Expression') is not None]

    def rule_feature_idices(self,rule):
        indices=[]
        for index, (feature, details) in enumerate(rule['features'].items()):
             expression = details.get('Expression')
             if expression is not None:
                if expression.get('type') == '==':
                    indices.append(index+1)
        return indices


    def find_features(self,step,indices):
        total_length = step * self.state_len
        result_array = []

        for position in range(total_length):
            if position % self.number_of_features == 0:
                result_array.extend([index + position for index in indices])

        return result_array
    
    def get_important_feature_list(self,step,rule):
        indices=self.important_feature_indices(rule)
        return self.find_features(step,indices)
    
    def get_rule_feature_list(self,step,rule):
        indices=self.rule_feature_idices(rule)
        return self.find_features(step,indices)
    


    def get_tailgaiting_feedback(self, traj, expl_type, count):

        tailgaiting_feedback=[]

        states= [s for s,a in traj]

        very_negative_signal=-1.5
        feedback_added = False

        for i,state in enumerate(states[1:], start=1):

            if feedback_added:
                break
                    
            for location in state[1:]:
                other_vehicle_x, other_vehicle_y=abs(location[1]),abs(location[2])
                if (other_vehicle_x<=self.thresholdX*100 and other_vehicle_y<=self.thresholdY)*100:
                    print("Very Negative Trajectory: {}".format(count))

                    rule = {
                        'quant': 's',
                        'time_steps':1,
                        'features': {
                            'Feature0': {'Expression': None},
                            'Feature1': {'Expression': {'abs': True, 'threshold': self.thresholdX, 'limit_sign': '<'}},
                            'Feature2': {'Expression': {'abs': True, 'threshold': self.thresholdY, 'limit_sign': '<'}},
                            'Feature3': {'Expression': None},
                            'Feature4': {'Expression': None}
                        }
                    }
                    start=max(0, i - (self.time_window//2))
                    end=min(len(traj), i +  (self.time_window//2) + 1)
                    important_features=[]
                    feedback=('s', traj[start:end], very_negative_signal,important_features,[rule],self.time_window) 
                    tailgaiting_feedback.append(feedback)
                    feedback_added = True
                    self.last_count = count  
                    break
        return tailgaiting_feedback

   
    def get_lane_feedback(self, traj, expl_type,count):
        lane_feedback=[] 
        thresholdY=0.1
        

        lanes = [s.flatten()[2] for s, a in traj]

        changed_lanes = [abs(lanes[i] - lanes[i-1]) > thresholdY if i >= 1 else False for i, _ in enumerate(lanes)] ## finding different lanes

        start = 0
        end = start + 2
        negative_label_assigned = False  

        while end < len(changed_lanes):
            while (end - start) <= self.time_window:
                if end >= len(changed_lanes):
                    break

                changed = sum(changed_lanes[(start+1):end]) >= self.max_changed_lanes
                if changed and changed_lanes[start+1]:  
                    print("Negative Trajectory number {}".format(count))
                    negative_label_assigned = True  
                    signal=-1
                    lane_feedback.append(('s', traj[start:end], signal, [2 + (i*self.state_len) for i in range(0, end-start)], {},end-start))
                    start = end  
                    end = start + 2
                    if expl_type == 'expl':
                        break
                else:
                    end += 1  
            start += 1  
            end = start + 2  

        if not negative_label_assigned:
            lowerbound, upperbound = 0, len(traj)
            if len(traj) > self.time_window:
                start = random.randint(0, len(traj) - self.time_window)  # Random start index
                lowerbound, upperbound = start, start + self.time_window
                signal=+1
                lane_feedback.append(('s', traj[lowerbound:upperbound], signal, [2 + (i*self.state_len) for i in range(0, upperbound-lowerbound)],{},upperbound-lowerbound ))
                print("Positive Trajectory Number: {}".format(count))

        
        return lane_feedback

    def get_equilibrium_feedback(self, best_traj, expl_type):
        negative_trajectories = []
        positive_trajectories = []
        negative_feedback_list = []
        positive_feedback_list = []

        for count, traj in enumerate(best_traj):
            negative_feedback, negative_label_assigned = self.get_negative_feedback(traj, expl_type, count)
            if negative_label_assigned:
                negative_feedback_list.append(negative_feedback)
                negative_trajectories.append(count)

        target_positive_feedback_count = int(self.epsilon * len(negative_feedback_list))
        available_trajectories_for_positive = [i for i in range(len(best_traj)) if i not in negative_trajectories]
        max_positive_feedback = min(target_positive_feedback_count, len(available_trajectories_for_positive))

        for count in available_trajectories_for_positive[:max_positive_feedback]:
            traj = best_traj[count]
            positive_feedback, positive_label_assigned = self.get_positive_feedback(traj, expl_type, count)
            if positive_label_assigned:
                positive_feedback_list.append(positive_feedback)
                positive_trajectories.append(count)

        return negative_feedback_list+positive_feedback_list
               
        

    def get_positive_lane_feedback(self,traj,expl_type,count):
        lowerbound, upperbound = 0, len(traj)
        if len(traj) > self.time_window:
            start = random.randint(0, len(traj) - self.time_window)  # Random start index
            lowerbound, upperbound = start, start + self.time_window
            signal=+1
            positive_lane_feedback=(('s', traj[lowerbound:upperbound], signal, [2 + (i*self.state_len) for i in range(0, upperbound-lowerbound)],{},upperbound-lowerbound ))
            print("Positive Trajectory Number: {}".format(count))
        return positive_lane_feedback,True


    def get_negative_change_lane_feedback(self, traj, expl_type,count):
        lane_feedback=[] 
        thresholdY=0.1
        

        lanes = [s.flatten()[2] for s, a in traj]

        changed_lanes = [abs(lanes[i] - lanes[i-1]) > thresholdY if i >= 1 else False for i, _ in enumerate(lanes)] ## finding different lanes

        start = 0
        end = start + 2
        negative_label_assigned = False  

        while end < len(changed_lanes):
            while (end - start) <= self.time_window:
                if end >= len(changed_lanes):
                    break

                changed = sum(changed_lanes[(start+1):end]) >= self.max_changed_lanes
                if changed and changed_lanes[start+1]:  
                    print("Negative-Lane Trajectory number {}".format(count))
                    negative_label_assigned = True  
                    signal=-1
                    lane_feedback=(('s', traj[start:end], signal, [2 + (i*self.state_len) for i in range(0, end-start)], {},end-start))
                    start = end  
                    end = start + 2
                    if expl_type == 'expl':
                        break
                else:
                    end += 1  
            start += 1  
            end = start + 2  
      
        return lane_feedback
    

    def get_right_lane_feedback(self,traj,expl_type,count):
        lanes = [s.flatten()[2] for s, a in traj]
        thresholdY=0.1
        right_lane_position=.75

        right_lane_feedback=[]
        right_lane_feedback_assigned=False

        def is_right_lane(lane_position, right_lane_position, thresholdY):
            return abs(lane_position - right_lane_position) < thresholdY
        
        right_lanes = [is_right_lane(lane, right_lane_position, thresholdY) for lane in lanes]

        for i in range(len(right_lanes) - 4):  # Subtract 4 because we are checking groups of 5
            if all(right_lanes[i:i+5]):
                signal=1
                right_lane_feedback_assigned=True
                right_lane_feedback=(('s', traj[i:i+5], signal, [2 + (i*self.state_len) for i in range(0, 5)], {},5))
                break  # Exit the loop after finding the first occurrence

        return  right_lane_feedback





    def get_feedback(self, best_traj, expl_type):
        feedback=[]
        tail_feedback_list=[]
        lane_change_feedback_list=[]
        right_lane_feedback_list=[]

        for count,traj in enumerate(best_traj):
            if self.run_tailgaiting:
                tail_feedback=self.get_tailgaiting_feedback( traj, expl_type,count)   
                if tail_feedback:
                    tail_feedback_list.append(tail_feedback[0]) 
                if not tail_feedback :
                    lane_change_feedback=self.get_negative_change_lane_feedback(traj,expl_type,count)
                    if lane_change_feedback:
                        lane_change_feedback_list.append(lane_change_feedback)
                    if not lane_change_feedback:
                        right_lane_feedback=self.get_right_lane_feedback(traj,expl_type,count)
                        if right_lane_feedback:
                            right_lane_feedback_list.append(right_lane_feedback)

            else:
                lane_feedback=self.get_lane_feedback(traj,expl_type,count)
                if lane_feedback:
                    lane_change_feedback_list.append(lane_feedback[0])

            
        feedback = [lst for lst in [tail_feedback_list, lane_change_feedback_list, right_lane_feedback_list] if lst]  # Filter out empty lists
        feedback = sum(feedback, [])  # Concatenate the non-empty lists
        feedback = [item for item in feedback if item]
        return feedback, True
    
    

    def get_equilibrium_feedback(self, best_traj, expl_type):
        feedback=[]
        tail_feedback_list=[]
        lane_trajectories_list=[]

        for count,traj in enumerate(best_traj):
            if self.run_tailgaiting:
                tail_feedback=self.get_tailgaiting_feedback( traj, expl_type,count)   
                if tail_feedback:
                    tail_feedback_list.append(tail_feedback[0]) 
                else :
                    lane_trajectories_list.append(traj)
           
            else:
                lane_trajectories_list.append(traj)
        
        lane_feedback_list=self.get_equilibrium_feedback(lane_trajectories_list, expl_type)
        feedback=tail_feedback_list+lane_feedback_list

        return feedback, True

    # def get_feedback(self, best_traj, expl_type):
    #     feedback = []
    #     tail_feedback_list = []
    #     lane_trajectories_list = []
    #     speed_feedback_list = []  # Initialize a list to hold speed feedbacks

    #     # First, filter trajectories for tailgating and lane feedback
    #     for count, traj in enumerate(best_traj):
    #         if self.run_tailgaiting:
    #             tail_feedback = self.get_tailgaiting_feedback(traj, expl_type, count)
    #             if tail_feedback:
    #                 tail_feedback_list.append(tail_feedback[0])
    #             else:
    #                 lane_trajectories_list.append(traj)
    #         else:
    #             lane_trajectories_list.append(traj)

    #     # Process trajectories for lane feedback
    #     lane_feedback_list = []
    #     for count, traj in enumerate(lane_trajectories_list):
    #         lane_feedback, lane_label_assigned = self.get_negative_feedback(traj, expl_type, count)
    #         if lane_label_assigned:
    #             lane_feedback_list.append(lane_feedback)
    #         else:
    #             # If a trajectory is not assigned a lane label, it's eligible for speed feedback
    #             speed_feedback = self.get_speed_feedback(traj, count)
    #             if speed_feedback:
    #                 speed_feedback_list.append(speed_feedback)

    #     # Combine feedbacks as needed
    #     feedback.extend(tail_feedback_list)
    #     feedback.extend(lane_feedback_list)
    #     feedback.extend(speed_feedback_list)

    #     return feedback,True

        

    def set_lambda(self, l):
        self.lmbda = l
    
    def set_epsilon(self, e):
        self.epsilon=e




#     def get_tailgaiting_feedback(self, best_traj, expl_type):
#         feedback_list=[]
#         for traj in best_traj:

#             thresholdX=1
#             thresholdY=0.2
#             states= [s for s,a in traj]

#             very_negative_signal=-4
#             feedback_added = False
            
#             for i,state in enumerate(states):

#                 if feedback_added:
#                     break
                        
#                 ego_vehicle_x, ego_vehicle_y = state[0][1], state[0][2]
#                 for location in state[1:]:
#                     other_vehicle_x, other_vehicle_y=location[1],location[2]
#                     if abs(other_vehicle_y-ego_vehicle_y) >0.1: ### vehicles are in different lanes
#                         continue
#                     if abs(other_vehicle_x-ego_vehicle_x) < thresholdX:
#                         print("Very Negative Trajectory")

#                         rule = {
#                                 'quant': 's',
#                                 'features': {
#                                     'Feature1': {
#                                         'Expression': {
#                                             'type': '-',  
#                                             'abs': True, 
#                                             'threshold': thresholdX ,
#                                             'limit_sign': '<' 

#                                         }
#                                     },
#                                     'Feature2': {
#                                         'Expression': {
#                                             'type':'-',
#                                             'threshold': thresholdY ,
#                                             'limit_sign': '<'
#                                         }
#                                     },
#                                     'Feature3': {
#                                         'Expression': None  
#                                     },
#                                     'Feature4': {
#                                         'Expression': None  
#                                     }
#                                 }
#                             }   
#                         start=max(0, i - (self.time_window//2))
#                         end=min(len(traj), i +  (self.time_window//2) + 1)
#                         important_features=self.get_rule_feature_list(end-start,rule)

#                         feedback=('s', traj[ start:end], very_negative_signal,important_features,rule,self.time_window) 
#                         feedback_list.append(feedback)
#                         feedback_added = True
#                         break
#         return feedback_list, True

   

 ##  new get binary feedback
    def get_Affirmative_lane_feedback(self, best_traj, expl_type):
        feedback_list = []
        count = 0
        for traj in best_traj:

            lanes = [s.flatten()[2] for s, a in traj]

            changed_lanes = [abs(lanes[i] - lanes[i-1]) > 0.1 if i >= 1 else False for i, _ in enumerate(lanes)]

            start = 0
            end = start + 2
            negative_label_assigned = False  

            while end < len(changed_lanes):
                while (end - start) <= self.time_window:
                    if end >= len(changed_lanes):
                        break

                    changed = sum(changed_lanes[(start+1):end]) >= self.max_changed_lanes
                    if changed and changed_lanes[start+1]:  
                        print("Negative Trajectory number {}".format(count))
                        negative_label_assigned = True  
                        feedback_list.append(('s', traj[start:end], signal, [2 + (i*self.state_len) for i in range(0, end-start)], {},end-start))
                        start = end  
                        end = start + 2
                        if expl_type == 'expl':
                            break
                    else:
                        end += 1  
                start += 1  
                end = start + 2  

            if not negative_label_assigned:
                
                lowerbound, upperbound = 0, len(traj)
                if len(traj) > self.time_window:
                    start = random.randint(0, len(traj) - self.time_window)  # Random start index
                    lowerbound, upperbound = start, start + self.time_window
                    signal=1
                    if random.random() < self.epsilon:
                        continue
                    print("Positive Trajectory Number {}".format(count))
                feedback_list.append(('s', traj[lowerbound:upperbound], signal, [2 + (i*self.state_len) for i in range(0, upperbound-lowerbound)],{},upperbound-lowerbound ))

            count += 1
        return feedback_list, True

