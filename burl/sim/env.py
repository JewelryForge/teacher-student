import math
import time
from collections import deque
from itertools import chain

import numpy as np
import pybullet
import pybullet_data
from pybullet_utils import bullet_client

from burl.rl.state import ExtendedObservation, Action
from burl.rl.task import BasicTask
from burl.sim.motor import MotorSim
from burl.sim.quadruped import A1, AlienGo, Quadruped
from burl.sim.terrain import makeTerrain
from burl.sim.tg import LocomotionStateMachine, vertical_tg
from burl.utils import make_cls, g_cfg, log_info, log_debug, unit, vec_cross
from burl.utils.transforms import Rpy, Rotation


class QuadrupedEnv(object):
    """
    Manage a simulation environment of a Quadruped robot, including physics and rendering parameters.
    Reads g_cfg.trn_type to generate terrains.
    """

    def __init__(self, make_robot=A1, make_task=BasicTask):
        self._gui = g_cfg.rendering
        self._env = bullet_client.BulletClient(pybullet.GUI if self._gui else pybullet.DIRECT) if True else pybullet
        self._env.setAdditionalSearchPath(pybullet_data.getDataPath())
        # self._loadEgl()
        if self._gui:
            self._prepareRendering()
        self._terrain = makeTerrain(self._env)
        self._robot: Quadruped = make_robot(self._env, self._terrain.getPeakInRegion(*make_robot.ROBOT_SIZE)[2])
        self._task = make_task(self)
        assert g_cfg.sim_frequency >= g_cfg.execution_frequency >= g_cfg.action_frequency

        self._setPhysicsParameters()
        self._initSimulation()
        self._num_action_repeats = int(g_cfg.sim_frequency / g_cfg.action_frequency)
        self._num_execution_repeats = int(g_cfg.sim_frequency / g_cfg.execution_frequency)
        log_debug(f'Action Repeats for {self._num_action_repeats} time(s)')
        log_debug(f'Execution Repeats For {self._num_execution_repeats} time(s)')
        self._resetStates()
        if self._gui:
            self._initRendering()
        self._action_buffer = deque(maxlen=10)

    def _resetStates(self):
        self._sim_step_counter = 0
        self._episode_reward = 0.0
        self._est_X = None
        self._est_Y = None
        self._est_Z = None
        self._est_height = 0.0
        self._external_force = np.array((0., 0., 0.))

    @property
    def client(self):
        return self._env

    @property
    def robot(self):
        return self._robot

    @property
    def terrain(self):
        return self._terrain

    @property
    def task(self):
        return self._task

    def initObservation(self):
        self._robot.updateObservation()
        return (self.makeObservation(False).standard(),
                self.makeObservation(True).standard())

    def _initSimulation(self):
        pass

    def _loadEgl(self):
        import pkgutil
        if egl := pkgutil.get_loader('eglRenderer'):
            log_info(f'LoadPlugin: {egl.get_filename()}_eglRendererPlugin')
            self._env.loadPlugin(egl.get_filename(), '_eglRendererPlugin')
        else:
            self._env.loadPlugin("eglRendererPlugin")

    def _prepareRendering(self):
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_RENDERING, False)
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_GUI, False)
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_TINY_RENDERER, False)
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_SHADOWS, False)
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_RGB_BUFFER_PREVIEW, False)
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_DEPTH_BUFFER_PREVIEW, False)
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_SEGMENTATION_MARK_PREVIEW, False)

    def _initRendering(self):
        if g_cfg.extra_visualization:
            self._contact_visual_shape = self._env.createVisualShape(shapeType=pybullet.GEOM_BOX,
                                                                     halfExtents=(0.03, 0.03, 0.03),
                                                                     rgbaColor=(0.8, 0., 0., 0.6))
            self._terrain_visual_shape = self._env.createVisualShape(shapeType=pybullet.GEOM_SPHERE,
                                                                     radius=0.01,
                                                                     rgbaColor=(0., 0.8, 0., 0.6))
            self._contact_obj_ids = []
            self._terrain_indicators = [self._env.createMultiBody(baseVisualShapeIndex=self._terrain_visual_shape)
                                        for _ in range(36)]
            self._force_indicator = -1
            self._external_force_buffer = self._external_force

        self._dbg_reset = self._env.addUserDebugParameter('reset', 1, 0, 0)
        self._reset_counter = 0

        self._last_frame_time = time.time()
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_GUI, True)
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_RENDERING, True)

    def _setPhysicsParameters(self):
        # self._env.setPhysicsEngineParameter(numSolverIterations=self._num_bullet_solver_iterations)
        self._env.setTimeStep(1 / g_cfg.sim_frequency)
        self._env.setGravity(0, 0, -9.8)

    def _updateRendering(self):
        if (current := self._env.readUserDebugParameter(self._dbg_reset)) != self._reset_counter:
            self._reset_counter = current
            self.reset()
        if g_cfg.sleeping_enabled:
            time_spent = time.time() - self._last_frame_time
            self._last_frame_time = time.time()
            time_to_sleep = self._num_action_repeats / g_cfg.sim_frequency - time_spent
            if time_to_sleep > 0:
                time.sleep(time_to_sleep)
        if g_cfg.moving_camera:
            yaw, pitch, dist = self._env.getDebugVisualizerCamera()[8:11]
            (x, y, _), z = self._robot.position, self._robot.STANCE_HEIGHT
            self._env.resetDebugVisualizerCamera(dist, yaw, pitch, (x, y, z))
        self._env.configureDebugVisualizer(pybullet.COV_ENABLE_SINGLE_STEP_RENDERING, True)
        if g_cfg.extra_visualization:
            for obj in self._contact_obj_ids:
                self._env.removeBody(obj)
            self._contact_obj_ids.clear()
            for cp in self._env.getContactPoints(bodyA=self._robot.id):
                pos, normal, normal_force = cp[5], cp[7], cp[9]
                if normal_force > 0.1:
                    obj = self._env.createMultiBody(baseVisualShapeIndex=self._contact_visual_shape,
                                                    basePosition=pos)
                    self._contact_obj_ids.append(obj)
            if self._external_force_buffer is not self._external_force:
                _force_indicator = self._env.addUserDebugLine(
                    lineFromXYZ=(0., 0., 0.), lineToXYZ=self._external_force / 50, lineColorRGB=(1., 0., 0.),
                    lineWidth=5, lifeTime=0,
                    parentObjectUniqueId=self._robot.id,
                    replaceItemUniqueId=self._force_indicator)
                if self._force_indicator != -1 and _force_indicator != self._force_indicator:
                    self._env.removeUserDebugItem(self._force_indicator)
                self._force_indicator = _force_indicator
                self._external_force_buffer = self._external_force

            positions = chain(*[self.getAbundantTerrainInfo(x, y, self._robot.rpy.y)
                                for x, y in self._robot.getFootXYsInWorldFrame()])
            for idc, pos in zip(self._terrain_indicators, positions):
                self._env.resetBasePositionAndOrientation(idc, posObj=pos, ornObj=(0, 0, 0, 1))

    def makeObservation(self, if_noisy=False) -> ExtendedObservation:
        eo = ExtendedObservation()
        r = self._robot
        eo.command = self._task.cmd
        eo.gravity_vector = r.getBaseAxisZ()
        eo.base_linear = r.getBaseLinearVelocityInBaseFrame(if_noisy)
        eo.base_angular = r.getBaseAngularVelocityInBaseFrame(if_noisy)
        eo.joint_pos = r.getJointPositions(if_noisy)
        eo.joint_vel = r.getJointVelocities(if_noisy)
        eo.joint_prev_pos_err = r.getJointPosErrHistoryFromIndex(-1, if_noisy)
        eo.joint_pos_err_his = np.concatenate((r.getJointPosErrHistoryFromMoment(-0.01, if_noisy),
                                               r.getJointPosErrHistoryFromMoment(-0.02, if_noisy)))
        eo.joint_vel_his = np.concatenate((r.getJointVelHistoryFromMoment(-0.01, if_noisy),
                                           r.getJointVelHistoryFromMoment(-0.02, if_noisy)))
        eo.joint_pos_target = r.getCmdHistoryFromIndex(-1)
        eo.joint_prev_pos_target = r.getCmdHistoryFromIndex(-self._num_action_repeats - 1)
        foot_xy = r.getFootXYsInWorldFrame()
        eo.terrain_scan = np.concatenate([self.getTerrainScan(x, y, r.rpy.y) for x, y in foot_xy])
        eo.terrain_normal = np.concatenate([self.getTerrainNormal(x, y) for x, y in foot_xy])
        eo.contact_states = r.getContactStates()[1:]
        eo.foot_contact_forces = r.getFootContactForces()
        eo.foot_friction_coeffs = [self.getTerrainFrictionCoeff(x, y) for x, y in foot_xy]
        eo.external_disturbance = self._external_force
        return eo

    def _estimateTerrain(self):
        self._est_X, self._est_Y, self._est_Z = [], [], []
        for x, y in self._robot.getFootXYsInWorldFrame():
            self._est_X.append(x)
            self._est_Y.append(y)
            self._est_Z.append(self.getTerrainHeight(x, y))
        x, y, _ = self._robot.position
        self._est_X.append(x)
        self._est_Y.append(y)
        self._est_Z.append(self.getTerrainHeight(x, y))
        self._est_height = np.mean(self._est_Z)

    def step(self, action):
        # NOTICE: ADDING LATENCY ARBITRARILY FROM A DISTRIBUTION IS NOT REASONABLE
        # NOTICE: SHOULD CALCULATE TIME_SPENT IN REAL WORLD; HERE USE FIXED TIME INTERVAL
        rewards = []
        reward_details = {}
        prev_action = self._action_buffer[-1] if self._action_buffer else np.array(self._robot.STANCE_POSTURE)
        self._action_buffer.append(action)
        for i in range(self._num_action_repeats):
            update_execution = self._sim_step_counter % self._num_execution_repeats == 0
            if update_execution:
                if g_cfg.use_action_interpolation:
                    weight = (i + 1) / self._num_action_repeats
                    current_action = action * weight + prev_action * (1 - weight)
                    torques = self._robot.applyCommand(current_action)
                else:
                    torques = self._robot.applyCommand(action)

            if g_cfg.add_disturbance:
                self._addRandomDisturbanceOnRobot()
            self._env.stepSimulation()
            self._sim_step_counter += 1
            self._estimateTerrain()
            rewards.append(self._task.calculateReward())
            for n, r in self._task.getRewardDetails().items():
                reward_details[n] = reward_details.get(n, 0) + r
            if update_execution:
                self._robot.updateObservation()
            if self._gui and g_cfg.single_step_rendering:
                self._env.configureDebugVisualizer(pybullet.COV_ENABLE_SINGLE_STEP_RENDERING, True)
                self._updateRendering()
        if self._gui and not g_cfg.single_step_rendering:
            self._updateRendering()
        for n in reward_details:
            reward_details[n] /= self._num_action_repeats
        is_failed = self._task.isFailed()
        time_out = not is_failed and self._sim_step_counter >= g_cfg.max_sim_iterations
        self._episode_reward += (mean_reward := np.mean(rewards))
        info = {'time_out': time_out, 'torques': torques, 'reward_details': reward_details,
                'episode_reward': self._episode_reward}
        if hasattr(self._terrain, 'difficulty'):
            info['difficulty'] = self._terrain.difficulty
        # log_debug(f'Step time: {time.time() - start}')
        # print(self.makeObservation(False).__dict__)
        # print(self.makeObservation(False).foot_contact_forces[(0, 3, 6, 9),].sum(),
        #       self.makeObservation(False).foot_contact_forces[(1, 4, 7, 10),].sum(),
        #       self.makeObservation(False).foot_contact_forces[(2, 5, 8, 11),].sum())
        return (self.makeObservation(False).standard(),
                self.makeObservation(True).standard(),
                mean_reward,
                is_failed or time_out,
                info)

    def _addRandomDisturbanceOnRobot(self):
        if self._sim_step_counter % g_cfg.disturbance_interval_steps == 0:
            self._applied_link_id = 0
            # self._applied_link_id = base_link_ids[np.random.randint(0, len(base_link_ids))]
            horizontal_force_magnitude = np.random.uniform(*g_cfg.horizontal_force_bounds)
            theta = np.random.uniform(0, 2 * math.pi)
            vertical_force_magnitude = np.random.uniform(*g_cfg.vertical_force_bounds)
            self._external_force = np.array((
                horizontal_force_magnitude * np.cos(theta),
                horizontal_force_magnitude * np.sin(theta),
                vertical_force_magnitude * np.random.choice((-1, 1))
            ))
            # print('Apply:', self._external_force)

        self._env.applyExternalForce(objectUniqueId=self._robot.id,
                                     linkIndex=self._applied_link_id,
                                     forceObj=self._external_force,
                                     posObj=(0.0, 0.0, 0.0),
                                     flags=pybullet.LINK_FRAME)

    def reset(self):
        # completely_reset = self._task.curriculumUpdate(self._sim_step_counter)
        completely_reset = False
        self._resetStates()
        self._task.reset()
        if completely_reset:
            self._env.resetSimulation()
            self._setPhysicsParameters()
            self._terrain.reset()
        self._robot.reset(self._terrain.getPeakInRegion(*self._robot.ROBOT_SIZE)[2],
                          reload=completely_reset)
        self._initSimulation()
        self._robot.updateObservation()
        # print(self.assembleObservation(False).__dict__)
        return (self.makeObservation(False).standard(),
                self.makeObservation(True).standard())

    def close(self):
        self._env.disconnect()

    def getActionMutation(self):
        if len(self._action_buffer) < 3:
            return 0.0
        actions = [self._action_buffer[-i - 1] for i in range(3)]
        return np.linalg.norm(actions[0] - 2 * actions[1] + actions[2]) * g_cfg.action_frequency ** 2

    def getAbundantTerrainInfo(self, x, y, yaw):
        interval = 0.1
        dx, dy = interval * np.cos(yaw), interval * np.sin(yaw)
        points = ((dx - dy, dx + dy), (dx, dy), (dx + dy, -dx + dy),
                  (-dy, dx), (0, 0), (dy, -dx),
                  (-dx - dy, dx - dy), (-dx, -dy), (-dx + dy, -dx - dy))
        return [(xp := x + dx, yp := y + dy, self.getTerrainHeight(xp, yp)) for dx, dy in points]

    def getTerrainScan(self, x, y, yaw):
        return [p[2] for p in self.getAbundantTerrainInfo(x, y, yaw)]

    def getTerrainHeight(self, x, y) -> float:
        return self._terrain.getHeight(x, y)

    def getSafetyHeightOfRobot(self) -> float:
        return self._robot.position[2] - self._est_height

    def getSafetyFootHeightsOfRobot(self) -> np.ndarray:
        foot_pos = [self._robot.getFootPositionInWorldFrame(i) for i in range(4)]
        return np.array([z - self.getTerrainHeight(x, y) - 0.02 for x, y, z in foot_pos])

    def getSafetyRpyOfRobot(self) -> Rpy:
        X, Y, Z = np.array(self._est_X), np.array(self._est_Y), np.array(self._est_Z)
        # Use terrain points to fit a plane
        A = np.zeros((3, 3))
        A[0, :] = np.sum(X ** 2), X @ Y, np.sum(X)
        A[1, :] = A[0, 1], np.sum(Y ** 2), np.sum(Y)
        A[2, :] = A[0, 2], A[1, 2], len(X)
        b = np.array((X @ Z, Y @ Z, np.sum(Z)))
        a, b, _ = np.linalg.solve(A, b)
        trn_Z = unit((-a, -b, 1))
        rot_robot = Rotation.from_quaternion(self._robot.orientation)
        trn_Y = vec_cross(trn_Z, rot_robot.X)
        trn_X = vec_cross(trn_Y, trn_Z)
        # (trn_X, trn_Y, trn_Z) is the transpose of rotation matrix, so there's no need to transpose again
        return Rpy.from_rotation(np.array((trn_X, trn_Y, trn_Z)) @ rot_robot)

    def getTerrainNormal(self, x, y) -> np.ndarray:
        return self._terrain.getNormal(x, y)

    def getTerrainFrictionCoeff(self, x, y) -> float:
        return 0.0


class TGEnv(QuadrupedEnv):
    tg_types = {'A1': make_cls(vertical_tg, h=0.08),
                'AlienGo': make_cls(vertical_tg, h=0.12)}

    def __init__(self, *args, **kwargs):
        super(TGEnv, self).__init__(*args, **kwargs)
        self._stm = LocomotionStateMachine(1 / g_cfg.action_frequency,
                                           make_tg=self.tg_types[self._robot.__class__.__name__])
        self._commands = None
        # self._filter = self._stm.flags

    def makeObservation(self, if_noisy=False) -> ExtendedObservation:
        eo: ExtendedObservation = super().makeObservation(if_noisy)
        eo.ftg_frequencies = self._stm.frequency
        eo.ftg_phases = np.concatenate((np.sin(self._stm.phases), np.cos(self._stm.phases)))
        return eo

    def step(self, action: Action):
        # 0 ~ 3 additional frequencies
        # 4 ~ 11 foot position residual
        self._stm.update(action.leg_frequencies)
        # self._filter += self._stm.flags
        priori = self._stm.get_priori_trajectory() + self._robot.STANCE_FOOT_POSITIONS
        des_pos = action.foot_pos_residuals.reshape((4, 3)) + priori
        use_horizontal_frame = False
        if not use_horizontal_frame:
            # self._commands = np.concatenate([self._robot.ik(i, pos, Quadruped.SHOULDER_FRAME)
            #                                  for i, pos in enumerate(des_pos)])
            self._commands = np.concatenate([self._robot.ik_analytic(i, pos, Quadruped.SHOULDER_FRAME)
                                             for i, pos in enumerate(des_pos)])
        else:
            h2b = self._robot.transformFromHorizontalToBase(True)
            offsets = ((0., -self._robot.LINK_LENGTHS[0], 0.),
                       (0., self._robot.LINK_LENGTHS[0], 0.),
                       (0., -self._robot.LINK_LENGTHS[0], 0.),
                       (0., self._robot.LINK_LENGTHS[0], 0.))
            des_pos = np.array([h2b @ (des_p + offset) for des_p, offset in zip(des_pos, offsets)])
            self._commands = np.concatenate([self._robot.ik_analytic(i, pos, Quadruped.HIP_FRAME)
                                             for i, pos in enumerate(des_pos)])

        if g_cfg.plot_trajectory:
            self.plotFootTrajectories(des_pos)

        # TODO: COMPLETE NOISY OBSERVATION CONVERSIONS
        return super().step(self._commands)

    def getLastCommand(self) -> np.ndarray:
        return self._commands

    def reset(self):
        self._stm.reset()
        return super().reset()

    def plotFootTrajectories(self, des_pos):
        from burl.utils import plotTrajectories
        if not hasattr(self, '_plotter'):
            self._plotter = plotTrajectories()
        for i, flag in enumerate(self._stm.cycles == 5):
            if flag:
                x, y, z = des_pos[i]
                self._plotter(i, (x, z), 'r')
                x, y, z = self._robot.getFootPositionInHipFrame(i)
                self._plotter(i, (x, z), 'b')

    def _initSimulation(self):  # for the stability of the beginning
        for _ in range(500):
            self._robot.updateObservation()
            self._robot.applyCommand(self._robot.STANCE_POSTURE)
            self._env.stepSimulation()


if __name__ == '__main__':
    from burl.utils import init_logger, set_logger_level

    g_cfg.moving_camera = False
    g_cfg.sleeping_enabled = True
    g_cfg.on_rack = False
    g_cfg.rendering = True
    g_cfg.trn_type = 'plain'
    g_cfg.add_disturbance = False
    g_cfg.test_mode = True
    g_cfg.single_step_rendering = False
    init_logger()
    set_logger_level('DEBUG')
    np.set_printoptions(precision=3, linewidth=1000)
    make_motor = make_cls(MotorSim)
    tg = True
    if tg:
        env = TGEnv(AlienGo)
        env.initObservation()
        for i in range(1, 100000):
            act = Action()
            # print(np.array(env.getSafetyFootHeightsOfRobot()))
            # env.robot.addDisturbanceOnBase((0, 0, 300))
            env.step(act)
            # time.sleep(0.05)
            # if i % 500 == 0:
            #     env.reset()
    else:
        env = QuadrupedEnv(AlienGo)
        env.initObservation()
        for i in range(1, 100000):
            env.step(env.robot.STANCE_POSTURE)
            print(env.robot.rpy)
