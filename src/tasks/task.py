import copy
import os
import random

from stable_baselines3 import DQN
import torch
from src.evaluation.evaluator import Evaluator
from src.feedback.feedback_processing import present_successful_traj, gather_feedback, augment_feedback_diff, \
    generate_important_features
from src.feedback.rule_feedback import give_rule_feedback
from src.reward_modelling.reward_model import RewardModel
from src.tasks.task_util import init_replay_buffer, check_dtype, check_is_unique
from src.visualization.visualization import visualize_feature


class Task:

    def __init__(self, env, model_path, dataset_path,model_env, model_expert, task_name, max_iter, env_config, model_config, eval_path, debugging,feedback_freq,expl_type='expl',auto=False, seed=0,run_tailgating=True,run_speed=True,lmbda=0.2,prefix='',user_study=False,load_iteration=0):
        self.model_path = model_path
        self.time_window = env_config['time_window']
        self.feedback_freq = feedback_freq 
        self.task_name = task_name
        self.model_config = model_config
        self.env_config = env_config
        self.model_env = model_env
        self.model_expert = model_expert
        self.env = env
        self.max_iter = max_iter
        self.eval_path = eval_path
        self.auto = auto
        self.seed = seed
        self.init_type = env_config['init_type']
        self.debugging=debugging
        self.run_speed=run_speed
        self.lmbda=lmbda
        self.prefix=prefix
        self.user_study=user_study
        self.load_iteration=load_iteration
        self.run_tailgating=run_tailgating

    
        # set seed
        random.seed(seed)

        self.init_model = self.model_env if self.init_type == 'train' else None
        ### need to add path variable
        init_data = init_replay_buffer(self.env, self.init_model, self.time_window, dataset_path,self.env_config['init_buffer_ep'], expl_type=expl_type,debugging=self.debugging,run_tailgaiting=run_tailgating,prefix=prefix)

        try:
            self.reward_model = RewardModel(self.time_window, env_config['input_size'],env_config['max_human_rew'],self.lmbda,load_iteration=load_iteration)
        except:
            self.reward_model = RewardModel(self.time_window, env_config['input_size'],999999,load_iteration=load_iteration)
        

        self.reward_model.buffer.initialize(init_data,load_iteration=load_iteration)

        # evaluator object
        self.evaluator = Evaluator(self.init_model, self.feedback_freq, env)

        # check the dtype of env state space
        self.state_dtype, self.action_dtype = check_dtype(self.env)

    def run(self, noisy=False, disruptive=False, experiment_type='regular', summary_type='best_summary', expl_type='expl', lmbda=0.2, prob=0,epsilon=0,prefix=''):
        finished_training = False
        iteration = self.load_iteration
        self.evaluator.reset_reward_dict()

        while not finished_training:
            print('Iteration = {}'.format(iteration))
            #model_path=self.model_path + '/{}_{}_{}/seed_{}_lmbda_{}_iter_{}'.format(experiment_type, summary_type, expl_type, self.seed, lmbda, iteration) #!! Only use for debugging  !!
            #if debugging:
            model_path=self.model_path + '/{}_{}_{}/seed_{}_lmbda_{}_epsilon_{}_iter_{}'.format(experiment_type, summary_type, expl_type, self.seed, lmbda, epsilon,iteration-1)
            if self.debugging:
                feedback_freq=100
            else:
                feedback_freq=self.feedback_freq

            self.env.set_epsilon(epsilon)
            try:    
                exploration_fraction = max(0.05, 0.8 - 0.1 * (iteration / 10))
                model = DQN.load(model_path, verbose=0, seed=random.randint(0, 100), exploration_fraction=exploration_fraction, env=self.env)
                print('Loaded saved model')
                # if it's not the first iteration reward model should be used
                self.env.set_shaping(True)
                self.env.set_lambda(lmbda)
                self.env.set_reward_model(self.reward_model)
                

            except FileNotFoundError:
                self.env.set_lambda(lmbda)
                model = DQN('MlpPolicy',
                            self.env,
                            seed=random.randint(0, 100),
                            **self.model_config)
                print('First time training the model')

            #--------------commenting out for debugging purposes
            print('Training DQN for {} timesteps'.format(feedback_freq))

            model.learn(total_timesteps=feedback_freq)
            model.save(self.model_path + '/{}_{}_{}/seed_{}_lmbda_{}_epsilon_{}_iter_{}'.format(experiment_type, summary_type, expl_type, self.seed, lmbda, epsilon,iteration))
            
 
            best_traj = present_successful_traj(model, self.env, summary_type, n_traj=10)
            if not self.user_study:
                # gather feedback trajectories
                feedback, cont = gather_feedback(best_traj, self.time_window, self.env, disruptive, noisy, prob, expl_type=expl_type, auto=self.auto)

            else:
                feedback,cont=self.env.get_user_study_feedback(best_traj)

            if iteration >= self.max_iter:
                cont = False

            if not cont:
                self.reward_model.update()
                if not noisy and not disruptive:
                    title = 'IRS.csv'
                else:
                    title = 'noisy_{}.csv'.format(prob) if noisy else 'disruptive_{}.csv'.format(prob)

                self.evaluator.evaluate(model, self.env, path=os.path.join(self.eval_path, title), lmbda=lmbda, seed=self.seed, epsilon=epsilon,write=True)
                break

            count=0    
            unique_feedback = []
            for feedback_type, feedback_traj, signal, important_features,rules, timesteps in feedback:
                count +=1
                important_features, actions, rules = generate_important_features(important_features, self.env.state_len, feedback_type, self.time_window, feedback_traj,rules)
                unique = check_is_unique(unique_feedback, feedback_traj, timesteps, self.time_window, self.env, important_features, expl_type,signal) ##signal has now been addeed to important features 

                if not unique:
                    continue
                else:
                    unique_feedback.append((feedback_traj, important_features, timesteps,signal))
                
                if not self.debugging:
                    length=10000

                else:
                    length =100
                # augment feedback for each trajectory
                D = augment_feedback_diff(feedback_traj,
                                          signal,
                                          copy.copy(important_features),
                                          rules,
                                          timesteps,
                                          self.env,
                                          self.time_window,
                                          actions,
                                          datatype=(self.state_dtype, self.action_dtype),
                                          expl_type=expl_type,
                                          length=length)

                # Update reward buffer with augmented data
                self.reward_model.update_buffer(D,
                                                signal,
                                                important_features,
                                                (self.state_dtype, self.action_dtype),
                                                actions,
                                                rules,
                                                iteration)
                    
            
            # Update reward model with augmented data
            self.reward_model.update()

            #self.evaluator.evaluate(model, self.env, path=self.prefix+'13_02.csv', lmbda=lmbda, seed=self.seed, write=True)

            rt=''

            if self.run_tailgating:
                rt='_tailgating'

            buffer_path='datasets/{}{}/buffer/{}_{}_{}/seed_{}_lmbda_{}_epsilon_{}'.format(self.task_name,rt,experiment_type, summary_type, expl_type, self.seed, lmbda, epsilon)
            buffer=self.reward_model.get_buffer()
            buffer_file_path=prefix+buffer_path+'data.pt'
            
            
            os.makedirs(buffer_path, exist_ok=True) 
            torch.save(buffer,buffer_file_path)



            reward_model_path='reward_models/{}{}/{}_{}_{}/seed_{}_lmbda_{}_epsilon_{}'.format(self.task_name,rt,experiment_type, summary_type, expl_type, self.seed, lmbda, epsilon)
            self.reward_model.save(reward_model_path)


            iteration += 1
            

            self.evaluator.evaluate(model, self.env, path=r'C:\Users\charl\Desktop\Dissertation\Technical Part\RePurpose_iters\13_02.csv', lmbda=lmbda, seed=self.seed, write=True)





            








