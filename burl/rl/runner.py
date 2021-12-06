import os
import time
from collections import deque

import numpy as np
import torch
import wandb

from burl.alg.ac import ActorCritic, ActorTeacher, Critic
from burl.alg.ppo import PPO
from burl.rl.task import BasicTask, RandomCmdTask
from burl.sim import TGEnv, A1, EnvContainerMultiProcess2, EnvContainer
from burl.utils import make_cls, g_cfg, g_dev, logger


class OnPolicyRunner:
    def __init__(self):
        make_robot = make_cls(A1)
        make_task = make_cls(BasicTask)
        # make_task = make_cls(RandomCmdTask)
        make_env = make_cls(TGEnv, make_task=make_task, make_robot=make_robot)
        if g_cfg.use_mp:
            self.env = EnvContainerMultiProcess2(make_env, g_cfg.num_envs)
        else:
            self.env = EnvContainer(make_env, g_cfg.num_envs)
        actor_critic = ActorCritic(ActorTeacher(), Critic()).to(g_dev)
        self.alg = PPO(actor_critic)

        self.current_iter = 0

    def learn(self):
        privileged_obs, obs = self.env.init_observations()
        critic_obs = privileged_obs if privileged_obs is not None else obs
        obs, critic_obs = obs.to(g_dev), critic_obs.to(g_dev)
        self.alg.actor_critic.train()  # switch to train mode (for dropout for example)

        reward_buffer, eps_len_buffer = deque(maxlen=10), deque(maxlen=10)
        cur_reward_sum = torch.zeros(self.env.num_envs, dtype=torch.float, device=g_dev)
        cur_episode_length = torch.zeros(self.env.num_envs, dtype=torch.float, device=g_dev)
        total_iter = self.current_iter + g_cfg.num_iterations
        reward_details = {}
        for it in range(self.current_iter + 1, total_iter + 1):
            start = time.time()
            with torch.inference_mode():
                for i in range(g_cfg.storage_len):
                    actions = self.alg.act(obs, critic_obs)
                    obs, privileged_obs, rewards, dones, infos = self.env.step(actions)
                    critic_obs = privileged_obs if privileged_obs is not None else obs
                    obs, critic_obs, rewards, dones = obs.to(g_dev), critic_obs.to(g_dev), rewards.to(
                        g_dev), dones.to(g_dev)

                    self.alg.process_env_step(rewards, dones, infos['time_out'])
                    self.env.reset(dones)

                    cur_reward_sum += rewards
                    cur_episode_length += 1
                    new_ids = (dones > 0).nonzero(as_tuple=False)
                    reward_buffer.extend(cur_reward_sum[new_ids][:, 0].cpu().numpy().tolist())
                    eps_len_buffer.extend(cur_episode_length[new_ids][:, 0].cpu().numpy().tolist())
                    cur_reward_sum[new_ids] = 0
                    cur_episode_length[new_ids] = 0
                    for k, v in infos['reward_details'].items():
                        reward_details[k] = reward_details.get(k, 0) + torch.sum(v, dtype=torch.float)
                if 'difficulty' in infos:
                    difficulty = torch.mean(infos['difficulty'])

                stop = time.time()
                collection_time = stop - start

                # Learning step
                start = stop
                self.alg.compute_returns(critic_obs)

            mean_value_loss, mean_surrogate_loss = self.alg.update()
            stop = time.time()
            learning_time = stop - start
            self.log(locals())
            reward_details.clear()
            if it % g_cfg.save_interval == 0:
                self.save(os.path.join(g_cfg.log_dir, f'model_{it}.pt'))

        self.current_iter += g_cfg.num_iterations
        self.save(os.path.join(g_cfg.log_dir, f'model_{self.current_iter}.pt'))

    def log(self, locs, width=25):
        logger.info(f"{'#' * width}")
        logger.info(f"Iteration {locs['it']}/{locs['total_iter']}")
        logger.info(f"Collection Time: {locs['collection_time']:.3f}")
        logger.info(f"Learning Time: {locs['learning_time']:.3f}")

        fps = int(g_cfg.storage_len * self.env.num_envs / (locs['collection_time'] + locs['learning_time']))
        logs = {'Loss/value_function': locs['mean_value_loss'],
                'Loss/surrogate': locs['mean_surrogate_loss'],
                # 'Loss/learning_rate': g_cfg.learning_rate,
                'Policy/mean_noise_std': self.alg.actor_critic.std.mean().item(),
                'Perform/total_fps': fps,
                'Perform/collection time': locs['collection_time'],
                'Perform/learning_time': locs['learning_time']}
        logs.update({f'Reward/{k}': v / (g_cfg.storage_len * g_cfg.num_envs)
                     for k, v in locs['reward_details'].items()})
        reward_buffer, eps_len_buffer = locs['reward_buffer'], locs['eps_len_buffer']
        if 'difficulty' in locs:
            logs.update({'Train/difficulty': locs['difficulty']}),
            logger.info(f"{'Difficulty:'} {locs['difficulty']:.3f}")
        if len(reward_buffer) > 0:
            reward_mean, eps_len_mean = np.mean(reward_buffer), np.mean(eps_len_buffer)
            logs.update({'Train/mean_reward': reward_mean,
                         'Train/mean_episode_length': eps_len_mean}),
            logger.info(f"{'Mean Reward:'} {reward_mean:.3f}")
            logger.info(f"{'Mean EpsLen:'} {eps_len_mean:.1f}")
        logger.info(f"Total Frames: {locs['it'] * g_cfg.num_envs * g_cfg.storage_len}")

        wandb.log(logs)

    def save(self, path, infos=None):
        if not os.path.exists(d := os.path.dirname(path)):
            os.makedirs(d)
        torch.save({
            'model_state_dict': self.alg.actor_critic.state_dict(),
            'optimizer_state_dict': self.alg.optimizer.state_dict(),
            'iter': self.current_iter,
            'infos': infos,
        }, path)

    def load(self, path, load_optimizer=True):
        loaded_dict = torch.load(path)
        self.alg.actor_critic.load_state_dict(loaded_dict['model_state_dict'])
        if load_optimizer:
            self.alg.optimizer.load_state_dict(loaded_dict['optimizer_state_dict'])
        self.current_iter = loaded_dict['iter']
        return loaded_dict['infos']

    def get_inference_policy(self, device=None):
        self.alg.actor_critic.eval()  # switch to evaluation mode (dropout for example)
        if device is not None:
            self.alg.actor_critic.to(device)
        return self.alg.actor_critic.act_inference
