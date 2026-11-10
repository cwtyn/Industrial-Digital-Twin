# -*- coding: utf-8 -*-
"""
Line_4 线配置 — 纯数据。由 codes/common.py 统一建图。
顶注: 现场口径见 lines/Line_4/Line_4输送描述.md。
Line_4 = A-E 五区多链辊床输送 + 7 条 CP 横移链 + 3 台**十字转台TT**(250/340/355, DrPos 控朝向) +
纯LT存位(205/215/300/305, tag=LT/LT, CP停靠在对位节点WT). 入口来自 Line_2/Line_3, 出口去 Exit_1/Exit_2/Exit_3。
"""
PREFIX = 'Line_4'

# 段号映射: 设备清单分组 A-E -> 输送段号 1A-1E
ZONES = {}
ZONES.update({d:'1A' for d in [5,10,15,20,25,40,45,50,60,65,70]})
ZONES.update({d:'1B' for d in [75,80,90,95,100,105,140,145,150,170,175,180,185]})
ZONES.update({d:'1C' for d in [120,125,130,155,200,205,210,215,220,270,275,280]})
ZONES.update({d:'1D' for d in [230,235,240,245,250,255,260]})
ZONES.update({d:'1E' for d in [290,295,300,305,310,320,325,340,345,350,355,360]})

# 辊床主线(各链"向前"顺序; RB 默认单向)。过渡RB/TT 原样进链。
CHAINS = [
    [80,95,140,175],                                  # 链1 (B)
    [75,100,145,180],                                 # 链2 (B)
    [50,70,105,150,185,230,235,240,245,250,255],      # 链3 (A->B->D, 250TT0°主链过, 尾255->Exit_3)
    [45,65,125],                                      # 链4 (A->C, 065过渡)
    [25,60,130,155,210],                              # 链5 (A->C, 060/155过渡)
    [220,270,275,280,295,320,340,345],                # 链6 (C->E, 340TT, 尾345->Exit_2)
    [310,325,355,360],                                # 链8 (E, 355TT, 尾360->Exit_1)
]

# 过渡(不可停车)辊床
TRANSITION_RB = {140,145,150,155,60,65,70,320,325,350}

# 十字转台脊柱(引擎 cross_tt): 250-260-340-350-355 两两双向 tt 边。
#   250TT(TT)/260(双向RB,可停)/340TT(TT)/350(过渡双向RB)/355TT(TT)。0°主链边走 1)链, 90°叉臂走此脊柱。
CROSS_TT = [250,260,340,350,355]
# 真正"十字型、靠 DrPos 信号横/竖显示"的 TT 集(供 viz)
CROSS_TT_DEVS = {250,340,355}

# 横移链 CP: (段号, 'CP编号', [站序列, 单向])；纯LT存位站自动停靠对位节点 WT(见 common)
CP_GROUPS = [
    ('1A','1A005CP',[10,15,20,25]),   # 入口横移(010..025 来自 Line_2)
    ('1A','1A040CP',[45,50]),         # 045(链4头)/050(链3头)
    ('1B','1B090CP',[95,100,105]),
    ('1B','1B170CP',[175,180,185]),
    ('1C','1C120CP',[125,130]),
    ('1C','1C200CP',[205,210,215,220]),   # 205/215 = 纯LT存位(WT)
    ('1E','1E290CP',[295,300,305,310]),   # 300/305 = 纯LT存位(WT)
]
# 跨CP联动桥接(纵向横向往复连续): cp(前组末) -> cp(后组首)。类型 cross。
EXTRA_EDGES = [
    ('Line_4_1A005CP_at_025','Line_4_1A040CP_at_045','cross'),
    ('Line_4_1B090CP_at_105','Line_4_1C120CP_at_125','cross'),
    ('Line_4_1B170CP_at_185','Line_4_1C200CP_at_205','cross'),
]

# 组合 LT 工位(=RB依附, X1=LT/X2=RB)；其中 POS3 为 3 位(高/中/低), 其余 2 位
LT_COMBOS = [10,15,20,25,45,50,95,100,105,125,130,175,180,185,210,220,295,310]
POS3 = {10,15,20,25,45,50,95,100,105,175,180,185,130,210,220}

# 纯LT存位(无RB, tag=LT/LT): 停车节点 ltp(dev)=...LT, 对位节点 wt(dev)=...WT
PURE_LT = [205,215,300,305]

# 转台 TT(90°/180°, 十字): 类型标注
TT_DEVS = [250,340,355]

# 入口高亮(链头/跨线喂入点)
ENTRIES = [10,15,20,25,45,50,75,80]

# 跨线接口(出/入/占位)统一由 interfaces.json 管理, build_line 启动时注入。

# 纵向带: 0=Line_1/Line_3 同带。Line_4 放 band1 = 与 Line_2 同带、排在 Line_2 右侧(车辆经 Line_2/Line_3 -> Line_4,
# 终点在最右); 不撑大 band0, 故 Line_2/Line_3 位置不变。
BAND = 1

# 流程式布局行(泳道); None=留空格。每行"入口在行首", 从行首向左接 Line_2/Line_3 出口, 行身向右延伸。
# 8 个入口逐行 y 对齐 8 个出口(每行 78px 等差, 行首同列成一条竖线):
#   r0 1B080 对 Line_3 440 / r1 1B075 对 Line_3 330 / r2 1A050 对 Line_2 640 / r3 1A045 对 Line_2 580
#   r4 1A025 对 Line_2 338 / r5 1A020 对 Line_2 335 / r6 1A015 对 Line_2 235 / r7 1A010 对 Line_2 135
# r8 链6 / r9 链8 为汇聚主链, 落到下方(链6 尾 345->Exit_2, 链8 尾 360->Exit_1)。
LAYOUT_ROWS = [
    [80, None, 95, 140, 175],                              # r0 链1 (1B080) 095 右移一格对齐 105/130/125
    [75, None, 100, 145, 180],                             # r1 链2 (1B075) 100 右移一格对齐 105/130/125
    [50, 70, 105, 150, 185, 230, 235, 240, 245, None, 250, 255],  # r2 链3 (1A050; 250@c10紧邻255, 260移入脊柱竖列)
    [45, 65, 125, None, 205],                             # r3 链4 (1A045, 尾125对齐105/130) + CP200 205@c4
    [25, 60, 130, 155, 210, None, None, None, None, None, 260],  # r4 链5(130对齐105/125; 210@c4) + 脊柱260@c10(居中250-340)
    [20, None, None, None, 215],                          # r5 (1A020) + CP200 215@c4
    [15, None, None, None, 220, 270, 275, 280, 295, 320, 340, 345],  # r6 (1A015) + 链6头220@c4(215正下方), 链6整列随后
    [10, None, None, None, None, None, None, None, 300],  # r7 (1A010) + CP290 300@c8
    [None, None, None, None, None, None, None, None, 305],# r8 CP290 305@c8
    [None, None, None, None, None, None, None, None, 310, 325, 355, 360],  # r9 链8(310@c8在305正下, 间距与其他滚床一致; 355落c10)
    # r10 无独立行: 脊柱连接RB 260/350 由 layout_flow 按相邻两TT几何中点自动置位(350 在 340(r6)-355(r9) 半行居中)
]

# Line_4 与 Line_2 同带(band1), 额外纵向偏移让 r0 对齐 Line_3 出口 y=78 (见 DY 计算)。
# 由 _tmp 计算得: yoff_band1 + DY = 78。当前 band 基准 588.9, 故 DY = 78 - 588.9 = -510.9。
DY = -510.9

# 决策点 DEC(分道决策, 数据源=决策表DB, 订阅 tag 走 make_extra_resolver kind='dec')。
#   迁移自 docs/DecPointList.md 的 Line_4 段(11 点)。字段对齐 demo:
#     id     = 图上标记节点 id(展示/订阅落盘键)
#     anchor = 布局锚点(取该站 RB 节点坐标, DEC 标记贴其旁不扰动; 注意 095Dec DB=LT095 而清单 Device 误写 090)
#     dx/dy  = 相对锚点的像素偏移(DEC 标记钉空位; 默认右上, 视重叠可调)
#     db     = 决策表 DB 符号(订阅决策表 + 落盘 dec 段用)
#     dest_map = [(道号, 后续设备节点)] 路由语义; 跨线 to(如 Line_2_1H338RB)为组合图节点, 仅语义不画边
DEC_POINTS = [
  dict(id='Line_4_1A025DEC', anchor='Line_4_1A025RB', dx=16, dy=-14, label='025DEC',
       db='L3L4_1A_LT025DecVisu',
       dest_map=[(1,'Line_4_1A060RB'),(2,'Line_4_1A060RB'),(3,'Line_4_1A045LT'),
                 (4,'Line_4_1A045LT'),(5,'Line_4_1A045LT'),(8,'Line_2_1H338RB')]),
  dict(id='Line_4_1A045DEC', anchor='Line_4_1A045RB', dx=16, dy=-44, label='045DEC', force=True,
       db='L3L4_1A_LT045DecVisu',
       dest_map=[(1,'Line_4_1A065RB'),(2,'Line_4_1A065RB'),(3,'Line_4_1A050RB'),
                 (4,'Line_2_1H580RB'),(5,'Line_4_1A050RB')]),
  dict(id='Line_4_1A050DEC', anchor='Line_4_1A050RB', dx=-16, dy=-14, label='050DEC',
       db='L3L4_1A_LT050DecVisu',
       dest_map=[(1,'Line_4_1A070RB'),(2,'Line_4_1A070RB'),(3,'Line_4_1A070RB'),
                 (5,'Line_2_1H640RB')]),
  dict(id='Line_4_1B095DEC', anchor='Line_4_1B095RB', dx=16, dy=-14, label='095DEC',
       db='L3L4_1B_LT095DecVisu',
       dest_map=[(1,'Line_4_1B100LT'),(2,'Line_4_1B100LT'),(3,'Line_4_1B140RB')]),
  dict(id='Line_4_1B100DEC', anchor='Line_4_1B100RB', dx=16, dy=-14, label='100DEC',
       db='L3L4_1B_LT100DecVisu',
       dest_map=[(1,'Line_4_1B105LT'),(2,'Line_4_1B105LT'),(3,'Line_4_1B145RB')]),
  dict(id='Line_4_1B105DEC', anchor='Line_4_1B105RB', dx=16, dy=-14, label='105DEC',
       db='L3L4_1B_LT105DecVisu',
       dest_map=[(1,'Line_4_1C125LT'),(2,'Line_4_1C125LT'),(3,'Line_4_1B150RB')]),
  dict(id='Line_4_1B185DEC', anchor='Line_4_1B185RB', dx=16, dy=-14, label='185DEC',
       db='L3L4_1B_LT185DecVisu',
       dest_map=[(1,'Line_4_1C205LT'),(2,'Line_4_1C205LT'),(3,'Line_4_1D230RB')]),
  dict(id='Line_4_1D250DEC', anchor='Line_4_1D250RB', dx=-16, dy=-14, label='250DEC',
       db='L3L4_1D_RB250DecVisu',
       dest_map=[(1,'Line_4_1D260LT'),(2,'Line_4_1D260LT'),(3,'Line_4_1D255RB')]),
  dict(id='Line_4_1E295DEC', anchor='Line_4_1E295RB', dx=16, dy=-14, label='295DEC',
       db='L3L4_1E_LT295DecVisu',
       dest_map=[(1,'Line_4_1E230RB'),(2,'Line_4_1E300LT'),(3,'Line_4_1E300LT')]),
  dict(id='Line_4_1E340DEC', anchor='Line_4_1E340RB', dx=16, dy=-14, label='340DEC',
       db='L3L4_1E_RB340DecVisu',
       dest_map=[(1,'Line_4_1E350RB'),(2,'Line_4_1E345RB'),(3,'Line_4_1D260RB')]),
  dict(id='Line_4_1E355DEC', anchor='Line_4_1E355RB', dx=16, dy=-14, label='355DEC',
       db='L3L4_1E_RB355DecVisu',
       dest_map=[(1,'Line_4_1E360RB'),(2,'Line_4_1E350RB'),(3,'Line_4_1E350RB')]),
]

# 演示用例(节点ID)
DEMOS = [
    ('入口010→015→020→025 (1A005CP横移)', 'Line_4_1A010RB', 'Line_4_1A025RB'),
    ('链5 入口025→060→130→155→210', 'Line_4_1A025RB', 'Line_4_1C210RB'),
    ('链4 045→065→125', 'Line_4_1A045RB', 'Line_4_1C125RB'),
    ('210→CP200→220 (链5尾入链6)', 'Line_4_1C210RB', 'Line_4_1C220RB'),
    ('链3 050→…→250TT→255(出口Exit_3)', 'Line_4_1A050RB', 'Line_4_1D255RB'),
    ('250TT 90°十字→260→340', 'Line_4_1D250RB', 'Line_4_1E340RB'),
    ('链6 220→…→340TT→345(出口Exit_2)', 'Line_4_1C220RB', 'Line_4_1E345RB'),
    ('340TT↔350↔355 (E十字 ×2)', 'Line_4_1E340RB', 'Line_4_1E355RB'),
    ('纯LT存位 210→215 (200CP前段)', 'Line_4_1C210RB', 'Line_4_1C215LT'),
]