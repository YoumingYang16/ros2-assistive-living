"""Explicit daily-living coverage and user-owned routine templates.

The catalog describes software routes, not proof that a physical care task has
been performed. Body-contact activities always create a request for a person.
"""
from __future__ import annotations

from copy import deepcopy


# id, label, implementation, example; kept as data so UI and audit use one source.
_SCENARIOS = [
    ("hydration", "喝水提醒", "reminder", "10分钟后提醒我喝水"),
    ("medication", "按已有安排提醒服药", "reminder", "30分钟后提醒我按既定安排服药"),
    ("meal_reminder", "用餐提醒", "reminder", "1小时后提醒我吃饭"),
    ("position_reminder", "自主调整姿势提醒", "reminder", "20分钟后提醒我调整姿势"),
    ("exercise_reminder", "按个人安排活动提醒", "reminder", "1小时后提醒我按自己的安排活动"),
    ("appointment", "预约与日程提醒", "reminder", "2小时后提醒我预约时间到了"),
    ("sleep", "睡前准备", "checklist", "开始睡前清单"),
    ("morning", "起床与晨间准备", "checklist", "开始晨间清单"),
    ("outdoor_prepare", "出门准备", "checklist", "开始出门清单"),
    ("return_home", "回家检查", "checklist", "开始回家清单"),
    ("shopping", "购物与日用品需求", "need", "把纸巾加入购物清单"),
    ("food_supply", "食品补充需求", "need", "把牛奶加入购物清单"),
    ("care_supply", "辅助用品补充", "need", "把护理垫加入购物清单"),
    ("checkin", "本人状态与舒适度记录", "checkin", "记录我现在感觉良好"),
    ("pain_report", "不适记录与人工帮助", "human_assistance", "我身体不舒服需要帮助"),
    ("toileting", "如厕协助", "human_assistance", "我需要如厕帮助"),
    ("bathing", "洗浴协助", "human_assistance", "我需要洗澡帮助"),
    ("dressing", "穿脱衣物协助", "human_assistance", "我需要穿衣帮助"),
    ("transfer", "床椅移乘协助", "human_assistance", "我需要移乘帮助"),
    ("feeding", "进食协助", "human_assistance", "我需要喂饭帮助"),
    ("positioning", "身体翻身或体位协助", "human_assistance", "我需要翻身帮助"),
    ("oral_care", "刷牙和口腔清洁协助", "human_assistance", "我需要刷牙帮助"),
    ("grooming", "梳洗与个人整理", "human_assistance", "我需要梳头帮助"),
    ("cooking", "做饭与热食协助", "human_assistance", "我需要做饭帮助"),
    ("dishwashing", "餐具清洁协助", "human_assistance", "我需要洗碗帮助"),
    ("laundry", "洗衣与晾晒协助", "human_assistance", "我需要洗衣帮助"),
    ("cleaning", "居室清洁协助", "human_assistance", "我需要打扫帮助"),
    ("waste", "垃圾处理协助", "human_assistance", "我需要倒垃圾帮助"),
    ("package", "快递与门外物品接收", "human_assistance", "我需要取快递帮助"),
    ("escort", "外出陪同", "human_assistance", "我需要陪同出门"),
    ("stairs", "台阶与跨越障碍协助", "human_assistance", "我需要上下楼帮助"),
    ("mobility_aid", "轮椅与辅助器具调整", "human_assistance", "我需要调整轮椅帮助"),
    ("visitor", "访客核实与应门协助", "human_assistance", "我需要应门帮助"),
    ("pet_care", "宠物照料协助", "human_assistance", "我需要照顾宠物帮助"),
    ("plant_care", "植物照料协助", "human_assistance", "我需要浇花帮助"),
    ("paperwork", "文件与表单协助", "human_assistance", "我需要填写表格帮助"),
    ("communication", "联系亲友的人工协助", "human_assistance", "我需要联系家人帮助"),
    ("companionship", "陪伴与交流请求", "human_assistance", "我需要有人陪伴"),
    ("charging", "手机及辅助设备充电协助", "human_assistance", "我需要充电帮助"),
    ("home_hazard", "烟雾、漏水等居家危险求助", "human_assistance", "家里漏水了需要帮助"),
    ("emergency", "紧急求助记录与待人工响应", "human_assistance", "紧急求助"),
    ("fetch_item", "物品取送与交接", "hardware_interface", "把手机从客厅送到卧室"),
    ("find_item", "寻找物品", "hardware_interface", "去客厅找手机"),
    ("lighting", "照明控制", "hardware_interface", "打开卧室灯"),
    ("climate", "风扇设备控制", "hardware_interface", "打开卧室风扇"),
    ("curtains", "窗帘控制", "hardware_interface", "打开客厅窗帘"),
    ("television", "电视电源控制", "hardware_interface", "打开客厅电视"),
    ("navigation", "室内移动", "hardware_interface", "去卧室"),
    ("stop", "随时中止机器人动作", "hardware_interface", "立即停止"),
    ("accessibility", "大字高对比与交互偏好", "profile", "打开大字模式"),
]

from .living_catalog import additions, routine_templates, coverage_audit, DOMAINS
_ADDITIONS, SCENARIO_ACTIONS, _DOMAIN_MAP = additions()
_SCENARIOS.extend(_ADDITIONS)

HELP_CATEGORIES = {key: label for key, label, mode, _ in _SCENARIOS if mode == "human_assistance"}
HELP_CATEGORIES["general"] = "其他生活协助"
REMINDER_CATEGORIES = {key: label for key, label, mode, _ in _SCENARIOS if mode == "reminder"}
REMINDER_CATEGORIES["general"] = "自定义提醒"

ROUTINES = {
    "morning": {"label": "晨间清单", "items": ["确认当前需要的人工协助", "按个人安排梳洗穿衣", "准备早餐和饮水", "查看今天的提醒与行程", "确认手机或呼叫设备在身边"]},
    "night": {"label": "睡前清单", "items": ["确认门窗和照明状态", "手机或呼叫设备放在可触及处", "饮水与个人用品准备好", "查看明天的安排", "确认需要的睡前人工协助"]},
    "outdoor": {"label": "出门清单", "items": ["确认出行路线和陪同人员", "携带手机、钥匙和必要证件", "检查辅助器具", "确认返程安排", "根据本人需要准备用品"]},
    "return_home": {"label": "回家清单", "items": ["确认已安全到家", "个人物品放在容易取用处", "手机和辅助设备按需充电", "记录需要补充的用品", "查看未完成提醒"]},
    "meal": {"label": "用餐清单", "items": ["确认餐食按本人既定要求准备", "准备餐具与饮水", "需要时请求进食协助", "用餐后确认本人状态", "按需安排餐具清理"]},
    "home_safety": {"label": "居家检查清单", "items": ["通道保持可通行", "呼叫设备在可触及处", "检查照明是否合适", "人工核对可能存在的居家危险", "记录需要维修或协助的事项"]},
}

ROUTINES.update(routine_templates())

DEFAULT_PROFILE = {
    "display_name": "使用者", "language": "zh-CN", "text_scale": 1.0,
    "high_contrast": False, "reduced_motion": False, "speech_enabled": True,
    "speech_rate": 1.0, "simple_mode": True, "switch_scan_seconds": 3.0,
    "timezone": "Asia/Hong_Kong", "confirmation_required": True,
}


def catalog() -> dict:
    modes = {
        "reminder": "持久化提醒、本人确认、稍后提醒、逾期记录",
        "checklist": "逐项由本人核对，未勾选不视为完成",
        "need": "持久化需求清单，不自动购物或付款",
        "checkin": "本人自述记录，不作诊断或治疗决定",
        "human_assistance": "本地人工协助请求，须人工确认处理；未接通信接口不会发送",
        "hardware_interface": "由机器人动作接口处理；设备接入和真实执行尚需独立验证",
        "profile": "保存使用者的显示与交互偏好",
        "incident": "本人异常报告、关联人工协助与后续核实；不是自动传感器检测",
        "equipment": "设备台账、本人维护记录及关联到时提醒；不是硬件检查",
        "handover": "汇总未结束待办与近期提醒，默认不对外分享",
        "wellbeing": "本人主动开启的确认等待；逾期建立本地协助请求，不推断健康状态",
    }
    result = {
        "categories": [{"id": key, "label": label, "domain": _DOMAIN_MAP[key], "domain_label": DOMAINS[_DOMAIN_MAP[key]],
                        "mode": mode if mode in {"hardware_interface", "human_assistance"} else "software",
                        "workflow_type": mode, "example": example, "examples": [example],
                        "description": modes[mode], "implementation": modes[mode],
                        "boundary": ("等待硬件接口接入和真实验证" if mode == "hardware_interface" else
                                     "本机记录，不等于消息送达或实际照护完成" if mode == "human_assistance" else
                                     "由本人确认记录，不推断实际活动已完成")} for key, label, mode, example in _SCENARIOS],
        "help_categories": deepcopy(HELP_CATEGORIES),
        "reminder_categories": deepcopy(REMINDER_CATEGORIES),
        "routines": deepcopy(ROUTINES),
        "capabilities": ["durable_reminders", "local_assistance_requests", "self_reported_checkins",
                         "daily_checklists", "needs_list", "accessibility_preferences", "local_contacts",
                         "reported_support_interruptions", "equipment_maintenance_records",
                         "local_care_handover", "opt_in_wellbeing_wait", "functional_domain_catalog"],
        "boundaries": ["不能穷尽个人生活中的所有需求，可添加自定义提醒、需求和人工协助请求。",
                       "本地请求不等于消息送达，不等于照护人员已响应。",
                       "身体接触照护通过人工协助流程处理，软件不实施移乘、洗浴或喂食。",
                       "服药提醒仅复述使用者既有安排，不推断剂量、不调整治疗。",
                       "没有真实硬件或外部通信的测试结果，不构成现实世界照护能力证明。"],
    }

    result["coverage_audit"] = coverage_audit(result["categories"])
    return result
