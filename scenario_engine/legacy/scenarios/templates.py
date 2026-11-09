"""Conservative scenario templates.

Templates only define parsing hints and known public historical windows.
They do not define expected returns. Returns are computed from local data.
"""

SCENARIO_TEMPLATES = [
    {
        "name": "中美贸易摩擦升级",
        "keywords": ["贸易战", "关税", "加征关税", "出口管制", "出口限制"],
        "event_type": "policy",
        "region": "GLOBAL",
        "affected_entities": ["出口链", "半导体", "制造业", "风险偏好"],
        "historical_windows": [
            ("2018-06-15", "2018-09-15", "2018 年中美贸易摩擦升级窗口"),
            ("2019-05-06", "2019-06-06", "2019 年贸易摩擦再升级窗口"),
        ],
        "missing": ["政策适用范围", "执行日期", "豁免清单", "海外反制措施"],
    },
    {
        "name": "疫情冲击",
        "keywords": ["疫情", "封控", "公共卫生"],
        "event_type": "macro",
        "region": "CN",
        "affected_entities": ["风险偏好", "消费", "医药", "交通运输"],
        "historical_windows": [
            ("2020-01-23", "2020-03-23", "2020 年疫情初期市场冲击窗口"),
        ],
        "missing": ["影响地区", "持续时间", "防控强度"],
    },
    {
        "name": "地产政策放松",
        "keywords": ["地产政策", "房地产放松", "限购放松", "地产刺激"],
        "event_type": "policy",
        "region": "CN",
        "affected_entities": ["地产链", "银行", "建材", "家电"],
        "historical_windows": [
            ("2022-11-11", "2023-01-11", "2022 年地产融资政策放松窗口"),
        ],
        "missing": ["政策城市范围", "融资端或需求端政策", "居民杠杆变化"],
    },
    {
        "name": "海外市场大跌",
        "keywords": ["美股暴跌", "海外市场暴跌", "全球股市暴跌", "外盘大跌"],
        "event_type": "global",
        "region": "GLOBAL",
        "affected_entities": ["风险偏好", "北向资金", "高估值成长股"],
        "historical_windows": [
            ("2020-03-09", "2020-03-23", "2020 年海外流动性冲击窗口"),
        ],
        "missing": ["海外跌幅", "是否伴随流动性风险", "汇率变化"],
    },
    {
        "name": "央行降息",
        "keywords": ["降息", "利率下行", "利率下降"],
        "event_type": "macro",
        "region": "CN",
        "affected_entities": ["利率", "银行", "地产", "成长股估值"],
        "historical_windows": [
            ("2019-11-05", "2019-12-05", "2019 年 MLF 利率下调后窗口"),
            ("2020-02-17", "2020-03-17", "2020 年政策利率下调后窗口"),
        ],
        "missing": ["降息品种", "是否伴随降准", "财政政策配合情况"],
    },
    {
        "name": "央行加息或流动性收紧",
        "keywords": ["加息", "利率上行", "流动性收紧"],
        "event_type": "macro",
        "region": "CN",
        "affected_entities": ["利率", "高估值成长股", "银行", "债券"],
        "historical_windows": [
            ("2021-01-25", "2021-02-25", "2021 年资金面收紧观察窗口"),
        ],
        "missing": ["加息品种", "持续时间", "是否为短端资金面冲击"],
    },
    {
        "name": "人民币汇率贬值",
        "keywords": ["人民币贬值", "汇率贬值", "美元兑人民币上升"],
        "event_type": "macro",
        "region": "CN",
        "affected_entities": ["汇率", "出口链", "外资流向", "航空"],
        "historical_windows": [
            ("2018-06-15", "2018-08-15", "2018 年人民币阶段性贬值窗口"),
            ("2022-04-18", "2022-05-18", "2022 年人民币快速贬值窗口"),
        ],
        "missing": ["汇率幅度", "外资流向", "是否伴随海外加息"],
    },
    {
        "name": "油价上涨",
        "keywords": ["油价上涨", "原油上涨", "油价上升"],
        "event_type": "global",
        "region": "GLOBAL",
        "affected_entities": ["原油", "航空", "化工", "通胀"],
        "historical_windows": [
            ("2022-02-24", "2022-03-24", "2022 年地缘冲突油价冲击窗口"),
        ],
        "missing": ["油价涨幅", "持续时间", "是否传导至通胀预期"],
    },
    {
        "name": "白酒行业监管",
        "keywords": ["白酒监管", "限制白酒", "消费税", "白酒限制"],
        "event_type": "industry",
        "region": "CN",
        "affected_entities": ["白酒", "消费", "高端消费"],
        "historical_windows": [
            ("2021-08-20", "2021-09-20", "2021 年白酒政策与情绪冲击窗口"),
        ],
        "missing": ["监管对象", "政策强度", "是否涉及税收"],
    },
    {
        "name": "新能源补贴退坡",
        "keywords": ["新能源补贴退坡", "补贴退坡", "新能源汽车补贴"],
        "event_type": "policy",
        "region": "CN",
        "affected_entities": ["新能源车", "电池", "汽车", "需求预期"],
        "historical_windows": [
            ("2019-03-26", "2019-04-26", "2019 年新能源补贴退坡窗口"),
            ("2022-12-01", "2023-01-03", "2022 年新能源补贴退出预期窗口"),
        ],
        "missing": ["补贴幅度", "适用车型", "是否有地方政策对冲"],
    },
]

DEFAULT_MISSING = ["事件发生日期", "持续时间", "影响范围", "是否已有市场预期"]
