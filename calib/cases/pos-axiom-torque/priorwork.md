# 查重快照时点:2026-01 底(RSS 2026 论文截止 01-30 前);所有记录以该时点可见文献为准

## I1
检索词:问题表述(torque estimation without torque sensor low-cost servo / sensorless force estimation manipulator / actuator dynamics sim-to-real low-cost);方法机制(differentiable simulation actuator identification / trajectory supervision torque surrogate / neural actuator model motor telemetry);相邻领域(payload calibration contact estimation industrial arm / force feedback teleoperation low-cost / friction hysteresis identification harmonic drive)
API 检索:https://export.arxiv.org/api/query?search_query=(abs:"actuator model" OR abs:"actuator dynamics" OR abs:"actuator network") AND abs:"differentiable" AND submittedDate:[202001010000 TO 202601302359](18 篇逐条核对,无一以位姿/编码器轨迹经可微仿真训 actuator 扭矩代理);abs:"sensorless" AND (abs:"force estimation" OR abs:"contact estimation" OR abs:"torque estimation") AND cat:cs.RO 同时窗(8 篇逐条核对,低成本域仅 DOB 路线 2507.06174,已入最近工作);https://export.arxiv.org/api/query?id_list=1901.08652,2011.04217,2304.13705,2402.11221,2409.03369,2301.13413,2502.17432,2410.12685
web 检索:sensorless external force estimation manipulator motor current → Shan 一系(2301.13413/2409.03369)与 MOB-Net,均工业臂/人形前提;differentiable simulation system identification robot dynamics trajectory → NeuralSim 及材质/接触参数辨识,无 actuator 扭矩代理;learning actuator model servo friction backlash hysteresis → 2410.12685(谐波驱动)及工业机器人摩擦辨识,无低成本伺服;low-cost arm torque estimation LeRobot(中英文)→ 未检出
工业界/非论文占位:LeRobot(GitHub huggingface/lerobot)与 SO-ARM100 生态工具止于运动学层(lerobot-kinematics 只做正逆运动学),无 actuator 模型/力估计项目;Feetech/Dynamixel 官方路线即电流模式+标称 Kt——固化 τ=Kt·I 线性假设;力感知的现行替代是换硬件:QDD 直驱臂(OpenArm)靠低减速比+电流直读扭矩,单价高一档,恰印证「要力感知就上更贵执行器」的默认
最近工作:
- Learning agile and dynamic motor skills for legged robots(actuator net)| https://arxiv.org/abs/1901.08652 | 2019-01 | ANYmal 12 个 SEA 的 actuator 网络:位置误差+速度历史→关节扭矩,>100 万样本 | 学习化 actuator 模型的可行性先例;监督为 SEA 弹簧形变直测扭矩真值,低成本伺服无此硬件
- NeuralSim: Augmenting Differentiable Simulators with Neural Networks | https://arxiv.org/abs/2011.04217 | 2020-11 | 可微引擎内嵌神经网络,轨迹状态匹配损失经梯度辨识未建模力 | 辨识对象是接触摩擦/被动阻尼等环境力,非 per-actuator 扭矩代理;无伺服臂与力感知
- Learning Fine-Grained Bimanual Manipulation with Low-Cost Hardware(ALOHA)| https://arxiv.org/abs/2304.13705 | 2023-04 | 低成本双臂遥操作+ACT,hobby 伺服生态可做精细操作 | 纯运动学层:不建动力学、不碰力感知;为本赌注提供平台与遥测接口,非竞争
- MOB-Net | https://arxiv.org/abs/2402.11221 | 2024-02 | 人形动量观测器+RNN 学模型不确定扭矩,IMU+编码器免传感器外扭矩估计 | 依赖精确刚体模型与浮动基动量观测框架,训练用真值外扭矩标签;非可微仿真、非低成本伺服
- Fine Robotic Manipulation without Force/Torque Sensor | https://arxiv.org/abs/2301.13413 | 2023-01 | 工业臂内部信号(位置/速度/电流)NN 回归外力 wrench,100μm 插孔 | 前提是工业驱动可靠的电流-扭矩特性;不产出动力学模型,无 sim-to-real 用途
- Fast Payload Calibration for Sensorless Contact Estimation Using Model Pre-training | https://arxiv.org/abs/2409.03369 | 2024-09 | Denso 工业臂电流残差 MLP 预训练+4 秒在线负载标定 | 同上工业前提,残差在电流空间;无可微仿真;头条(低成本伺服位姿监督扭矩代理)未触
- FACTR: Force-Attending Curriculum Training | https://arxiv.org/abs/2502.17432 | 2025-02 | 力反馈双边遥操作+力感课程训练接触丰富策略 | 消费侧用力信号,力取自臂上现成关节扭矩读数——恰依赖本赌注要删的「臂上有可靠扭矩读数」前提;不解决力信号从哪来
- Physics-Informed Learning for the Friction Modeling of High-Ratio Harmonic Drives | https://arxiv.org/abs/2410.12685 | 2024-10 | ergoCub 人形 PINN 摩擦辨识,扭矩真值由刚体模型+本体信号间接构造 | 监督依赖精确刚体模型(人形级硬件),辨识止于摩擦项;无可微仿真、无外力估计
- Sensorless 4-Channel Bilateral Teleoperation for Low-Cost Manipulators | https://arxiv.org/abs/2507.06174 | 2025-07 | 低成本臂 DOB 速度/外力估计+解析非线性补偿,力反馈遥操作与力增强演示 | 低成本域免传感器力估计的最近占位:解析 DOB 路线,精度被手工辨识模型覆盖不了的摩擦/迟滞封顶;无学习化 actuator 模型、无可入仿真的动力学、头条未触
最强反例:Shan 一系(2301.13413 + 2409.03369)——已在真机做成「免 F/T 传感器的外力/接触估计」,与本 idea 的力感知收益最近。但其成立前提恰是本赌注要删的东西:工业驱动可靠的电流-扭矩特性与整机标定数据;且不产出可入仿真的动力学模型,未触「位姿轨迹经可微仿真监督」。头条(低成本伺服域 pose-only 扭矩代理+随附力感知)与其不冲突,差异足以支撑 clear-accept。
重叠判定:low —— 「低成本伺服 actuator 扭矩代理以位姿轨迹经可微仿真监督」在论文(API 时窗扫描 18+8 篇)、GitHub(lerobot/SO-ARM100 生态)、中英文社区均零检出;近邻密集但全部落在四条既有轴上:扭矩真值监督(actuator-net/MOB-Net/谐波 PINN)、工业电流标定(Shan 一系)、解析 DOB(2507.06174)、纯运动学低成本生态(ALOHA/FACTR)——监督信号替换这一环空白。力感知收益轴按 SA 净增量规则以 2507.06174 为 baseline:可记分的是解析补偿覆盖不了的摩擦/迟滞残差精度与「产出可入仿真的动力学模型」两项新增;头条与动力学收益轴零占据。
实读篇数:9(2011.04217、2410.12685、1901.08652、2409.03369 实读方法与实验节;其余实读摘要)
编号自查:是(8 篇经 arXiv API id_list 实际命中并核对标题与日期,2507.06174 直接来自 API 检索命中;abs 页逐篇复核作者/v1 日期)
裂缝证据核验(仅删承重假设形态,逐条覆盖该 idea 自报的 URL):
- https://arxiv.org/abs/2011.04217 | 核验:相符 —— 实读方法节确认:神经增强嵌入可微引擎内部,仅以仿真-实测轨迹匹配损失经端到端梯度训练,辨识出摩擦/被动阻尼等未建模力;「动力学须逐量直测」已被轨迹监督替代
- https://arxiv.org/abs/2410.12685 | 核验:相符 —— 实读方法节确认:ergoCub 无关节扭矩传感("eliminates the need for dedicated setups and joint torque sensors"),扭矩真值由刚体模型+电流/IMU 间接构造供 PINN 摩擦辨识;「扭矩必须直测」在松动(注:其监督仍依赖精确刚体模型,低成本伺服不具备,裂缝真实但未覆盖本赌注)
