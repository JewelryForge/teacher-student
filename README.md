# teacher-student

_Nov20_
- base_twist的测量值从世界坐标系改为基体坐标系
- 对action、TG的基准频率clip
- 修改了原PPO中的noise_std（1.0是否太大了？）
- is_safe的判定更加严格，但仍需要继续考虑（序列的指标）
- 提高速度奖励的权重，训练中可以很快学会站定，但倾向于向前倒

_Nov21_
- multiprocess的效率太低，如果每步step都创建线程，花的时间是单线程的15~20倍（32环境4线程）
  - 创建线程 ～5ms
  - 循环时间（+start）～40ms
  - 等待时间（join）3e-4～40ms
  - 单线程32环境step ～100ms
- 取消对action的clip，否则会学到一个很诡异的余差（z>0），导致不再迭代；考虑直接在action超限时terminate
- 研究ppo的算法

_Nov22_
- getJointStates的力和力矩的意义不明，接触力改用getContactPoints
- 重构了底层代码，删除legged_gym意义不明的代码

_Nov23_
- 重新找回legged_gym的代码，其使用GAE估计优势
- 增加TG输入和输出的系数，增加输入的标准化，使训练初期变得稳定
- 增大了最长回合数

_Nov24_
- 学习率1e-3有尖峰，1e-4跌落后上不去（经验池24）
- 经验池128，有学习的迹象，最终还是崩掉了
- 将步态初始值设为固定的($0$, $\pi/2$, $\pi$, $3\pi/2$)，开始稳定但会跌落
- 最终在100和10之间跳跃，尚不知为何

![image-20211124194917480](/home/jewel/Workspaces/teacher-student/README.assets/image-20211124194917480.png)

_Nov25_
- 取消TG频率改变，获取经验一次学习4次，在很短时间内学会站立不动
- 恢复TG频率改变，成啦！参数：8env，128storage，learn4次，奖励0.06linear，0.05angular，0.03stable，学习率1e-4，在1e6次左右学到，但有转弯的倾向，且到后面似乎不太稳定
```python
num_learning_epochs=4,
num_mini_batches=1,
clip_param=0.2,
gamma=0.995,
lam=0.95,
value_loss_coef=1.0,
entropy_coef=0.0,
learning_rate=1e-4,
max_grad_norm=1.0,
use_clipped_value_loss=True,
schedule="fixed",
desired_kl=0.01,
device='cuda',
```
# 目标

_Stage2_
- 数据增加噪声
- terrain curriculum