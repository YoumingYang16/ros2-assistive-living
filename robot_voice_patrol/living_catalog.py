"""A bounded domain audit; categories describe support routes, not autonomy.

The domains are a product checklist informed by WHO activity/participation
areas. They are not WHODAS questions, clinical scoring or certification.
"""
from copy import deepcopy

DOMAINS = {
    "self_care":"个人起居与身体照护", "health_support":"既有健康安排与就医支持",
    "nutrition":"饮食与用品", "household":"家务与居家环境", "mobility":"室内移动与物品操作",
    "community":"出行、旅行与社区参与", "communication":"沟通、社交与个人自主",
    "work_learning":"工作、学习与文娱", "responsibilities":"家庭责任与事务办理",
    "equipment":"辅助器具与设备维护", "continuity":"照护连续性与异常情况",
    "interaction":"机器人交互与无障碍操作",
}

# id, label, domain, canonical direct request. Similar phrases are aliases, not counts.
HELP = [
    ("continence","失禁用品与个人清洁协助","self_care","我需要更换护理用品帮助"),
    ("intimate_hygiene","经期或私密卫生协助","self_care","我需要私密卫生帮助"),
    ("medical_equipment","既有医疗辅助设备的人工操作协助","health_support","我需要医疗辅助设备操作帮助"),
    ("prescription_supply","既有处方续配与药品补充协助","health_support","我需要按已有处方补充药品帮助"),
    ("measurement_help","按既有安排测量与记录协助","health_support","我需要测量记录帮助"),
    ("therapy_help","按已有康复计划的人力协助","health_support","我需要按已有康复计划活动帮助"),
    ("open_packaging","食品与用品包装开启协助","nutrition","我需要打开包装帮助"),
    ("diet_preference","向照护者说明既定饮食要求","nutrition","我需要说明饮食要求帮助"),
    ("grocery_storage","采购品接收与存放协助","nutrition","我需要存放采购物品帮助"),
    ("bedding","床品更换与床铺整理协助","household","我需要更换床单帮助"),
    ("home_repair","家庭设施维修协调","household","我需要维修家庭设施帮助"),
    ("accessible_layout","可触及物品与通行空间调整","household","我需要调整无障碍布局帮助"),
    ("temperature_help","空调与供暖设备人工调整","household","我需要调整空调温度帮助"),
    ("assistive_setup","辅助输入与通信设备设置","equipment","我需要设置辅助输入设备帮助"),
    ("accessible_transport","无障碍交通安排协助","community","我需要安排无障碍交通帮助"),
    ("venue_access","目的地可达性与设施核实","community","我需要核实目的地无障碍设施帮助"),
    ("travel_accommodation","外宿与旅行支持安排","community","我需要安排无障碍住宿帮助"),
    ("remote_meeting","远程会议与线上交流操作协助","work_learning","我需要参加远程会议帮助"),
    ("reading_help","信件、屏幕和纸质资料阅读协助","work_learning","我需要阅读资料帮助"),
    ("recreation","兴趣活动与文娱用品操作协助","work_learning","我需要进行兴趣活动帮助"),
    ("parenting","育儿相关人力协助","responsibilities","我需要照顾孩子帮助"),
    ("dependent_care","其他家庭成员照护安排","responsibilities","我需要安排家人照护帮助"),
    ("bills_admin","账单核对与事务办理协助","responsibilities","我需要核对账单帮助"),
    ("online_order","购物下单过程的人工协助","responsibilities","我需要网上购物操作帮助"),
    ("private_conversation","保留隐私的交流安排","communication","我需要私下交流帮助"),
    ("personal_boundaries","表达身体接触与照护边界","communication","我需要表达照护边界帮助"),
    ("support_interruption","支持中断与备用人员协调","continuity","我需要备用照护人员帮助"),
]

ROUTINES = {
    "medical_visit":("就医准备","health_support",["核对本人确认的预约时间","准备已有记录与必要证件","核实交通和陪同安排","写下本人想询问的问题","确认返程与途中需要的协助"]),
    "travel":("旅行准备","community",["核实交通与住宿可达性","确认途中协助人员","按既有安排准备个人用品","确认辅助器具与充电安排","留下本人同意的备用联系方案"]),
    "work_study":("工作学习准备","work_learning",["将资料与输入设备放在可触及位置","确认文字、语音或辅助输入可用","安排本人需要的休息","核实会议或课程时间","记录需要他人提供的操作帮助"]),
    "social":("社交活动准备","communication",["由本人选择参与的活动","核实地点与时间","按本人意愿确定陪同","准备所需沟通方式","确认返回与休息安排"]),
    "outage_prepare":("停电备用准备","continuity",["记录本人可用的备用照明","核实呼叫设备备用电源","由人工核实关键辅助设备的备用安排","记录本人同意的替代联系人","将必要用品放在可触及位置"]),
    "evacuation_prepare":("应急撤离准备","continuity",["由本人和协助者确认现有应急方案","核实可行出口与需要的人力","确认辅助器具和必要物品","明确集合及联系安排","记录方案核实日期，不进行自动搬运"]),
    "therapy_prepare":("个人活动准备","health_support",["查看本人已有活动安排","准备既定辅助器具","确认是否需要人员协助","按本人意愿记录舒适度","活动结果由本人记录，不推断疗效"]),
    "visitor_prepare":("访客准备","communication",["由本人确认愿意见谁","约定时间与交流方式","按本人选择安排隐私","需要时请求应门协助","会面结束由本人确认"]),
}

BASE_DOMAINS = {
    "self_care": "sleep morning toileting bathing dressing transfer feeding positioning oral_care grooming",
    "health_support": "medication position_reminder exercise_reminder appointment checkin pain_report",
    "nutrition": "hydration meal_reminder shopping food_supply care_supply",
    "household": "cooking dishwashing laundry cleaning waste plant_care lighting climate curtains television",
    "mobility": "fetch_item find_item navigation stairs mobility_aid",
    "community": "outdoor_prepare return_home escort",
    "communication": "communication companionship visitor",
    "work_learning": "paperwork",
    "responsibilities": "package pet_care",
    "equipment": "charging",
    "continuity": "home_hazard emergency",
    "interaction": "stop accessibility",
}

def additions():
    from .living_operations import INCIDENTS
    scenarios=[];actions={};domains={key:domain for domain,keys in BASE_DOMAINS.items() for key in keys.split()}
    for key,label,domain,example in HELP:
        scenarios.append((key,label,"human_assistance",example));domains[key]=domain
        actions[example]={"op":"assistance.create","category":key,"detail":example}
    for key,(label,domain,items) in ROUTINES.items():
        example="开始"+label+"清单";identifier="routine_"+key
        scenarios.append((identifier,label,"checklist",example));domains[identifier]=domain
        actions[example]={"op":"checklist.start","routine":key}
    for key,(label,_) in INCIDENTS.items():
        example="报告"+label;identifier="incident_"+key
        scenarios.append((identifier,label+"报告与后续核实","incident",example));domains[identifier]="continuity"
        actions[example]={"op":"incident.create","category":key}
    for key,label,mode,domain,example,action in [
        ("care_handover","待办与照护交接摘要","handover","continuity","生成照护交接摘要",{"op":"handover.build"}),
        ("equipment_register","辅助设备台账与维护提醒","equipment","equipment","登记我的轮椅",{"op":"equipment.add","title":"轮椅","category":"mobility_aid"}),
        ("wellbeing_check","本人自选定时平安确认","wellbeing","continuity","开始30分钟平安确认",{"op":"wellbeing.start","seconds":1800}),
    ]:
        scenarios.append((key,label,mode,example));domains[key]=domain;actions[example]=action
    return scenarios, actions, domains

def routine_templates():
    return {key:{"label":value[0]+"清单","items":deepcopy(value[2])} for key,value in ROUTINES.items()}

def coverage_audit(categories):
    _,_,mapping=additions()
    return {"scope":"成年人行动不便；居家及与日常生活相关的外出、工作学习和社会活动支持",
            "method":"按功能领域审查；同义句不计作新增功能；每个已列场景有明确入口及处理边界",
            "universal_exhaustiveness_claim":False,
            "stopping_criteria":["全部领域已审查","所有列出场景具有可验证入口","无法自主执行的活动明确转人工或接口","自定义提醒、清单、需求和一般协助承接个人差异"],
            "domains":[{"id":key,"label":label,"scenario_ids":[c["id"] for c in categories if mapping[c["id"]]==key]} for key,label in DOMAINS.items()],
            "remaining_dependencies":["设备型号驱动与真实环境部署","个人实际需求与专业照护方案","真人试用和真实硬件验证"],
            "reference":"https://www.who.int/classifications/international-classification-of-functioning-disability-and-health/who-disability-assessment-schedule"}
