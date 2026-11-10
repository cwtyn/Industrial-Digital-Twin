# -*- coding: utf-8 -*-
"""
Line_1 线配置 — 纯数据。由 codes/common.py 统一建图。
顶注: 链/CP/LT/TT/SL/TC/入口出口 等现场口径见 lines/Line_1/Line_1输送描述.md。
节点ID一律带线前缀(Line_1_), 段号映射见 ZONES; 粒度=设备号(带完整段号跨线唯一)。
"""
PREFIX = 'Line_1'

ZONES = {}
ZONES.update({d:'1A' for d in [10,15,20,25,30,35,45,50,60,65,160,165]})
ZONES.update({d:'1B' for d in [70,75,80,85,90,95,100,170,175,180,185,190,195,200]})
ZONES.update({d:'1C' for d in [105,110,115,120,125,130,205,210,215,220,225,230]})
ZONES.update({d:'1D' for d in [135,235,255,260,265,295,300,320,325,330]})
ZONES.update({d:'1E' for d in [350,355,360,365,385,390,395,400]})
ZONES.update({d:'1F' for d in [370,402,406,408,410,412,414,415,425,430,445,450,455,460]})

# 辊床主线(各链为"向前"顺序; RB 默认单向沿链向前, 无反向回链)
CHAINS = [
    [10,30,35,50,160,165,170,175,180,185,190,195,200,205,210,215,220,225,230,255,235,300,325],
    [15,20,25,45,60,65,70,75,80,85,90,95,100,105,110,115,120,125,130,260,265,135,295,320,350,355,360,365,370,406,412,445],
    [330,385,390,395,400,402],
    [408,414,430],   # TC402 位置1: 402(经TC) -> 408 -> 414 -> 430
    [410,415,425],   # TC402 位置2: 402(经TC) -> 410 -> 415 -> 425
]

# 流程式布局行(泳道); None=留空格以对齐上下行。约定(A1/链3, 链1, 链2, 链4, 链5):
#   A1=330链置顶 -> 325(链1尾) -> 320(链2) 竖排(310CP 三个受控站, 从上到下 330/325/320)
#   链1在"255 后插 1 空", 使 235/300/325 与 链2 的 135/295/320 对齐
LAYOUT_ROWS = [
    # r0: 链3(顶) + 右侧三列顶部(列A/B/C)
    #   链3: 330 对齐(23列), 高于 325/320; 再接 385,390,395,400,402
    #   列A col29=410, 列B col30=415, 列C col31=425 (TC 虚拟位/各列顶)
    [None]*23 + [330,385,390,395,400,402,410,415,425],
    # r1: 链1(010 在 015 上方) + 右侧列中间层
    #   010 链 0..23(325@23 与 330/320 竖排); 再 408@29, 414@30, 430@31
    [10,30,35,50,160,165,170,175,180,185,190,195,200,205,210,215,220,225,230,255,None,235,300,325] + [None]*5 + [408,414,430],
    # r2: 链2(015) 0..28(320@23 与 330/325 竖排, 235/135.300/295 对齐) + 列底
    #   406@29(列A底), 412@30(列B底), 445@31(列C第3层)
    [15,20,25,45,60,65,70,75,80,85,90,95,100,105,110,115,120,125,130,260,265,135,295,320,350,355,360,365,370] + [406,412,445],
    # r3-r5: 列C 继续向下 450,455,460 (从上到下 425/430/445/450/455/460)
    [None]*31 + [450],
    [None]*31 + [455],
    [None]*31 + [460],
]

# 横移链 CP: (段号, 'CP编号', [站序列, 单向])
CP_GROUPS = [
    ('A','1A005CP',[10,15]),
    ('A','1A040CP',[45,50]),
    ('D','1D250CP',[255,260]),      # 原描述误写 025, 实为 250
    ('D','1D310CP',[320,325,330]),  # 320->325->330, 打通 链2/链1/链3
    ('F','1F404CP',[406,408,410]),
    ('F','1F420CP',[425,430]),
    ('F','1F440CP',[445,450,455,460]),
]
# 420CP↔440CP 联动桥接(430 续接 445, 单向): 425/430 -> 445/450/455/460
EXTRA_EDGES = [('Line_1_1F420CP_at_430','Line_1_1F440CP_at_445', 'cross')]

# 组合 LT(=RB), 仅 CP 对接工位需要
LT_COMBOS = [10,15,45,50,255,260,320,325,330,406,408,410,425,430,445,450,455,460]
POS3 = {320,325,408,430,445,450,455}   # 3位(高/中/低); 其余为2位(高/低)

# 功能件(绑定同名RB)
SL_DEVS = {355, 390}
TT_DEVS = {365, 400, 135}   # 135TT 转90° -> Line_6 外接(此退化在 interfaces.json 统一登记)

# 横移车 TC402
TC = {'dev': 402, 'docks': {1: 408, 2: 410}}   # pos1(Reverse)->408, pos2(Forward)->410

# 过渡(不可停车)辊床
TRANSITION_RB = {265, 360, 370, 412, 414, 415}

# 跨线接口(出/入/转台外接)统一由 interfaces.json 管理, `build_line` 启动时注入到
# EXT_EXITS / EXT_INBOUND / EXT_TURN(本模块不再手写, 避免与注册表不一致)。

# 入口(链头)
ENTRIES = [10, 15]                      # 1A010, 1A015 (330 由 CP310 从链2 喂入, 非入口)

# 演示用例(节点ID): 用于 build_line --demo 连通性验证
DEMOS = [
    ('010->链1尾325(经255/235/300)', 'Line_1_1A010RB', 'Line_1_1D325RB'),
    ('010->CP005->015->链2->412(过渡RB)', 'Line_1_1A010RB', 'Line_1_1F412RB'),
    ('Line_2 1E487RB 来车->445->CP440->460', 'Line_2_1E487RB', 'Line_1_1F460RB'),
    ('015->链2->445->Line_2出口1E487', 'Line_1_1A015RB', 'Line_2_1E487RB'),
    ('010->链1->CP310->330->链3->TCpos1->430->Line_3_1A230', 'Line_1_1A010RB', 'Line_3_1A230RB'),
    ('010->链1->CP310->330->链3->TCpos2->425->Line_3_1A340', 'Line_1_1A010RB', 'Line_3_1A340RB'),
    ('015->链2->445->CP440->460->Line_2出口1A005', 'Line_1_1A015RB', 'Line_2_1A005RB'),
    ('015->链2->135TT转向->Line_6外接1H445', 'Line_1_1A015RB', 'Line_6_1H445RB'),
    ('010->链1->255->CP250->260(链1喂入链2)', 'Line_1_1A010RB', 'Line_1_1D260RB'),
    ('015->链2->320->CP310->330(链2喂入链3)', 'Line_1_1A015RB', 'Line_1_1D330RB'),
    ('420CP+440CP联动: 430->CP440->445->460', 'Line_1_1F408RB', 'Line_1_1F460RB'),
    ('TC接车限pos1: 400->402->pos1->408(链4)', 'Line_1_1E400RB', 'Line_1_1F408RB'),
]

# 决策点 DEC(分道决策表 DB, 迁移自 docs/DecPointList.md 的 Line_1 段, 10 点)。
# 与 Line_4 同一套机制: config 声明 → make_line_viz 注入 DEC 标记 + run_runtime 展开订阅
# (build_dec_subscriptions kind='dec') → runtime_state 落盘 dec 段。字段/切片在 workshop_source。
#   id     = 图上标记节点 id(展示/订阅落盘键)
#   anchor = 布局锚点(RB 站节点, DEC 标记贴其旁, 不扰动原布局)
#   dx/dy  = 相对锚点像素偏移(默认右上, 重叠时可调)
#   db     = 决策表 DB 符号(Line_1 属 DecPoint_ 家族, 与 Line_4 的 xxxDecVisu 家族同字段路径)
#   dest_map = [(道号, 后续设备名)] 路由语义(纯显示/路径推算, 不必是真图节点)
DEC_POINTS = [
  dict(id='Line_1_1A010DEC', anchor='Line_1_1A010RB', dx=16, dy=-14, label='010DEC',
       db='L3L1_1A_DecPoint_CP005_RB010',
       dest_map=[(1,'Line_1_1A015LT'),(2,'Line_1_1A030RB'),(3,'Line_1_1A015LT')]),
  dict(id='Line_1_1A045DEC', anchor='Line_1_1A045RB', dx=16, dy=-14, label='045DEC',
       db='L3L1_1A_DecPoint_RB045',
       dest_map=[(1,'Line_1_1A060RB'),(2,'Line_1_1A050LT'),(3,'Line_1_1A060RB')]),
  dict(id='Line_1_1D255DEC', anchor='Line_1_1D255RB', dx=16, dy=-14, label='255DEC',
       db='L3L1_1D_DecPoint_RB255',
       dest_map=[(1,'Line_1_1D235RB'),(2,'Line_1_1D235RB'),(3,'Line_1_1D260LT')]),
  dict(id='Line_1_1D320DEC', anchor='Line_1_1D320RB', dx=16, dy=-14, label='320DEC',
       db='L3L1_1D_DecPoint_RB320',
       dest_map=[(1,'Line_1_1E350RB'),(2,'Line_1_1D325LT'),(3,'Line_1_1E350RB')]),
  dict(id='Line_1_1F406DEC', anchor='Line_1_1F406RB', dx=16, dy=-14, label='406DEC',
       db='L3L1_1F_DecPoint_RB406',
       dest_map=[(1,'Line_1_1F412RB'),(2,'Line_1_1F412RB'),(3,'Line_1_1F412RB'),(4,'Line_1_1F412RB'),
                 (5,'Line_1_1F412RB'),(6,'Line_1_1F412RB'),(7,'Line_1_1F408LT'),(8,'Line_1_1F408LT')]),
  dict(id='Line_1_1F408DEC', anchor='Line_1_1F408RB', dx=16, dy=-44, label='408DEC', force=True,
       db='L3L1_1F_DecPoint_RB408',
       dest_map=[(1,'Line_1_1F414RB'),(2,'Line_1_1F414RB'),(3,'Line_1_1F414RB'),(4,'Line_1_1F414RB'),
                 (5,'Line_1_1F414RB'),(6,'Line_1_1F414RB'),(7,'Line_1_1F410LT'),(8,'Line_1_1F414RB')]),
  dict(id='Line_1_1F425DEC', anchor='Line_1_1F425RB', dx=16, dy=-14, label='425DEC',
       db='L3L1_1F_DecPoint_RB425',
       dest_map=[(1,'Line_1_1F430LT'),(2,'Line_1_1F430LT'),(3,'Line_1_1F430LT'),(4,'Line_1_1F430LT'),
                 (5,'Line_1_1F430LT'),(6,'Line_1_1F430LT'),(7,'Line_3_1A340RB'),(8,'Line_1_1F430LT')]),
  dict(id='Line_1_1F430DEC', anchor='Line_1_1F430RB', dx=16, dy=-44, label='430DEC', force=True,
       db='L3L1_1F_DecPoint_RB430',
       dest_map=[(1,'Line_1_1F445LT'),(2,'Line_1_1F445LT'),(3,'Line_1_1F445LT'),(4,'Line_1_1F445LT'),
                 (5,'Line_1_1F445LT'),(6,'Line_3_1A230RB'),(8,'Line_1_1F445LT')]),
  dict(id='Line_1_1F445DEC', anchor='Line_1_1F445RB', dx=16, dy=-14, label='445DEC',
       db='L3L1_1F_DecPoint_RB445',
       dest_map=[(1,'Line_1_1F450LT'),(2,'Line_1_1F450LT'),(3,'Line_1_1F450LT'),(4,'Line_1_1F450LT'),
                 (5,'Line_2_1E487RB'),(8,'Line_1_1F450LT')]),
  dict(id='Line_1_1F450DEC', anchor='Line_1_1F450RB', dx=-72, dy=27, label='450DEC', force=True,
       db='L3L1_1F_DecPoint_RB450',
       dest_map=[(1,'Line_1_1F455LT'),(2,'Line_1_1F455LT'),(3,'Line_1_1F455LT'),(4,'Line_2_1E450RB'),
                 (8,'Line_1_1F455LT')]),
]