from __future__ import annotations

import math
import random
from typing import Type

import numpy as np

from burl.rl.curriculum import CURRICULUM_PROTOTYPE, CentralizedCurriculum
from burl.rl.reward import *
from burl.sim.plugins import Plugin, StatisticsCollector
from burl.sim.terrain import Terrain, Plain
from burl.utils import g_cfg

__all__ = ['BasicTask', 'RandomLinearCmdTask', 'RandomCmdTask', 'get_task', 'CentralizedTask']


class BasicTask(RewardRegistry):
    def __init__(self, env, cmd=(1.0, 0.0, 0.0)):
        super().__init__(np.asarray(cmd), env, env.robot)
        for reward, weight in g_cfg.rewards_weights:
            self.add_reward(reward, weight)
        self.set_coeff(0.5)

        self.plugins: list[Plugin] = []
        if g_cfg.test_mode:
            self.load_plugin(StatisticsCollector())

    cmd = property(lambda self: self._cmd)
    env = property(lambda self: self._env)
    robot = property(lambda self: self._robot)

    def make_terrain(self, terrain_type: str) -> Terrain:
        if terrain_type == 'plain':
            terrain_inst = Plain()
            terrain_inst.spawn(self._env.client)
        elif terrain_type == 'curriculum':
            for plg in self.plugins:
                if hasattr(plg, 'generate_terrain'):
                    terrain_inst = plg.generate_terrain(self._env.client)
                    break
            else:
                raise RuntimeError('Not a TerrainCurriculum instance is registered')
        elif terrain_type == 'rough':
            raise NotImplementedError
        elif terrain_type == 'slope':
            raise NotImplementedError
        else:
            raise RuntimeError(f'Unknown terrain type {terrain_type}')
        return terrain_inst

    def load_plugin(self, plugin: Plugin):
        self.plugins.append(plugin)

    def on_init(self):
        """Called back before env.initObservation"""
        for plg in self.plugins:
            plg.on_init(self, self._robot, self._env)

    def on_simulation_step(self):
        """Called back after every simulation step"""
        for plg in self.plugins:
            plg.on_simulation_step(self, self._robot, self._env)

    def on_step(self):
        """Called back after every env.step"""
        info = {}
        for plg in self.plugins:
            if plg_info := plg.on_step(self, self._robot, self._env):
                info |= plg_info
        return info

    def reset(self):
        """Called back before env resets"""
        for plg in self.plugins:
            plg.on_reset(self, self._robot, self._env)

    def is_failed(self):
        r, _, _ = self._robot.rpy
        safety_h = self._env.getTerrainBasedHeightOfRobot()
        h_lb, h_ub = self._robot.STANCE_HEIGHT * 0.5, self._robot.STANCE_HEIGHT * 1.5
        if (safety_h < h_lb or safety_h > h_ub or r < -np.pi / 3 or r > np.pi / 3 or
                self._robot.getBaseContactState()):
            return True
        # joint_diff = self._robot.getJointPositions() - self._robot.STANCE_POSTURE
        # if any(joint_diff > g_cfg.joint_angle_range) or any(joint_diff < -g_cfg.joint_angle_range):
        #     return True
        return False


# class RandomLeftRightTask(BasicTask):
#     def __init__(self, env):
#         self.update_interval = 1500
#         self.last_update = 0
#         self.last_cmd = 0
#         super().__init__(env, (0., 1., 0.))
#
#     def reset(self):
#         self.last_update = 0
#         self._cmd = np.array((0., 1., 0.))
#         super().reset()
#
#     def on_step(self):
#         if self._env.sim_step >= self.last_update + self.update_interval:
#             self._cmd = np.array((0., 1., 0.) if self.last_cmd else (0., -1., 0.))
#             self.last_cmd = 1 - self.last_cmd
#             self.last_update = self._env.sim_step
#         super().on_step()


class RandomLinearCmdTask(BasicTask):
    """Randomly updates linear command"""

    def __init__(self, env, seed=None):
        random.seed(seed)
        # self.stop_prob = 0.2
        self.interval_range = (1000, 2500)
        self.update_interval = random.uniform(*self.interval_range)
        self.last_update = 0
        super().__init__(env, self.random_cmd())

    def random_cmd(self):
        # if random.random() < self.stop_prob:
        #     return np.array((0., 0., 0.))
        yaw = random.uniform(0, 2 * np.pi)
        return np.array((math.cos(yaw), math.sin(yaw), 0))

    def reset(self):
        self.update_interval = random.uniform(*self.interval_range)
        self.last_update = 0
        self._cmd = self.random_cmd()
        super().reset()

    def on_step(self):
        if self._env.sim_step >= self.last_update + self.update_interval:
            self._cmd = self.random_cmd()
            self.last_update = self._env.sim_step
            self.update_interval = random.uniform(*self.interval_range)
        return super().on_step()


class RandomCmdTask(RandomLinearCmdTask):
    """Randomly updates command"""

    def random_cmd(self):
        yaw = random.uniform(0, 2 * np.pi)
        return np.array((math.cos(yaw), math.sin(yaw), random.choice((-1., 0, 0, 1.))))
        # return np.array((math.cos(yaw), math.sin(yaw), clip(random.gauss(0, 0.5), -1, 1)))


class CentralizedTask(object):
    """A wrapper of Task class for centralized curricula"""

    def __init__(self):
        self.curriculum_prototypes: list[CURRICULUM_PROTOTYPE] = []
        aggressive = g_cfg.test_mode or g_cfg.aggressive
        buffer_len = g_cfg.num_envs * 2
        if g_cfg.use_centralized_curriculum:
            from burl.rl.curriculum import CentralizedDisturbanceCurriculum, CentralizedTerrainCurriculum
            if g_cfg.add_disturbance:
                self.curriculum_prototypes.append(
                    CentralizedDisturbanceCurriculum(buffer_len=buffer_len, aggressive=aggressive))
            if g_cfg.trn_type == 'curriculum':
                self.curriculum_prototypes.append(
                    CentralizedTerrainCurriculum(buffer_len=buffer_len, aggressive=aggressive))
        else:
            from burl.rl.curriculum import DisturbanceCurriculum, TerrainCurriculum
            if g_cfg.add_disturbance:
                self.curriculum_prototypes.append(DisturbanceCurriculum(aggressive))
            if g_cfg.trn_type == 'curriculum':
                self.curriculum_prototypes.append(TerrainCurriculum(aggressive))

    def make_distribution(self, task_class: Type[BasicTask], *args, **kwargs):
        def _make_distribution(env):
            task_inst = task_class(env, *args, **kwargs)
            for crm in self.curriculum_prototypes:
                task_inst.load_plugin(crm.make_distribution())
            return task_inst

        return _make_distribution

    def update_curricula(self):
        for crm in self.curriculum_prototypes:
            if isinstance(crm, CentralizedCurriculum):
                crm.check_letter_box()


def get_task(task_type: str):
    if task_type == 'basic':
        return BasicTask
    elif task_type == 'randLn':
        return RandomLinearCmdTask
    elif task_type == 'randCmd':
        return RandomCmdTask
    # elif task_type == 'randLR':
    #     return RandomLeftRightTask
    else:
        raise RuntimeError(f"Unknown task type '{task_type}'")
