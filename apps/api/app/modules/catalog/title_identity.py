import html
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

_OPEN_TO_GROUP = {"[": "[]", "【": "[]", "(": "()", "（": "()"}
_CLOSE_TO_GROUP = {"]": "[]", "】": "[]", ")": "()", "）": "()"}
_MAX_BRACKET_SPAN = 160

_EVENT_PREFIX_RE = re.compile(
    r"^\s*[（(\[【](?:"
    r"c\d+[a-z]?|comiket\s*\d+|comic\s*market\s*\d+|"
    r"ac\d+|エアコミケ\s*\d+|gw超同人祭|"
    r"comitia\s*\d+|コミティア\s*\d+|comic1\s*☆?\s*\d*|"
    r"sc\d+|例大祭\s*\d*|紅楼夢\s*\d*|红楼梦\s*\d*|ff\d+|cr\d+|"
    r"同人誌|同人志|同人cg集|成年コミック|一般コミック"
    r")[）)\]】]\s*",
    re.IGNORECASE,
)
_EVENT_ONLY_RE = re.compile(
    r"^(?:c\d+[a-z]?|comiket\s*\d+|comic\s*market\s*\d+|"
    r"comitia\s*\d+|コミティア\s*\d+|comic1\s*☆?\s*\d*|"
    r"ac\d+|エアコミケ\s*\d+|gw超同人祭|sc\d+|例大祭\s*\d*|紅楼夢\s*\d*|红楼梦\s*\d*|ff\d+|cr\d+|"
    r"同人誌|同人志|同人cg集|成年コミック|一般コミック)$",
    re.IGNORECASE,
)

VERSION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"汉化|漢化|翻译|翻譯|翻訳|嵌字|重嵌|个人汉化|個人漢化|个人翻译|個人翻譯|机翻|機翻",
        "汉化/翻译",
    ),
    (r"上色|全彩|カラー|full[ -]?colou?r", "上色/全彩"),
    (
        r"无码|無碼|无修正|無修正|去码|去碼|無修|无修|\buncensored\b|\bdecensored\b",
        "无码/无修正",
    ),
    (r"ai(?:去码|去碼|(?:无|無)?修正|uncensored|翻译|翻譯|汉化|漢化)", "AI 处理"),
    (r"\bdl\s*版|\bdigital\b", "DL 版"),
    (r"扫图|掃圖|\bscan(?:lation|ned|s)?\b", "扫图版"),
    (r"\boriginal\b|原版|オリジナル", "原版"),
    (r"\bincomplete\b|未完成|缺[页頁]|落丁", "不完整版"),
    (r"(?<![a-z0-9])v\s*\d+(?:\.\d+)?\b", "修订版"),
)

_STATUS_NOISE_RE = re.compile(
    r"完[结結]|连载|連載|\bongoing\b|\bcompleted\b|\bincomplete\b",
    re.IGNORECASE,
)
_BRACKET_VERSION_RE = re.compile(
    r"中国翻訳|中国翻译|中文翻譯|中文汉化|英訳|英譯|"
    r"高画質|高画质|修正版|無修|无修|"
    r"^(?:中文|chinese|english|japanese|korean|raw|dl|complete|translated|漢化|汉化)$",
    re.IGNORECASE,
)
_CHAPTER_RANGE_RE = re.compile(
    r"(?:全\s*\d+\s*[話话回]|\b\d+\s*[-~～]\s*\d+\s*[話话回])",
    re.IGNORECASE,
)
_SINGLE_CHAPTER_RE = re.compile(
    r"(?:第\s*(\d+(?:\.\d+)?)\s*(?:話|话|화|回)|"
    r"(?<![a-z0-9\u3040-\u30ff\u3400-\u9fff])(\d+(?:\.\d+)?)\s*(?:話|话|화)|"
    r"\bch(?:apter)?\.?\s*(\d+(?:\.\d+)?)\b)",
    re.IGNORECASE,
)
_NON_CONTEXT_BRACKET_RE = re.compile(
    r"^(?:"
    r"\d+|"
    r"(?:第\s*)?\d+(?:\.\d+)?\s*(?:話|话|화|回|ch(?:apter)?\.?)|"
    r"(?:part|vol\.?|act|ep\.?)\s*(?:\d+|[ivx]+)|"
    r"前[編篇]|中[編篇]|後[編篇]|后[編篇]|完結[編篇]|完结[編篇]|"
    r"[上中下][巻卷]?|総集編|总集篇|番外編?|外[伝传]|続編?|续篇?|体験版|体验版|試読版?|试读版?|"
    r"sample|preview|trial|sequel|anthology|omnibus"
    r")$",
    re.IGNORECASE,
)

_COMPILATION_RE = re.compile(
    r"総集編|総集版|总集篇|总集版|再録集|再录集|傑作選|杰作选|\banthology\b|\bomnibus\b",
    re.IGNORECASE,
)
_COMPILATION_BRACKET_RE = re.compile(r"合集|\bcollection\b", re.IGNORECASE)
_SEQUEL_RE = re.compile(
    r"(?:^|[\s\[【（(〜~・:：\-_/])"
    r"(?:続[編刊]?|续[篇集]?|續[篇集]?|\bsequel\b)"
    r"(?=$|[\s\]】）)〜~・!！?？])",
    re.IGNORECASE,
)
_SPINOFF_RE = re.compile(
    r"番外編?|外[伝传]|\bspin[- ]?off\b",
    re.IGNORECASE,
)
_SAMPLE_RE = re.compile(
    r"体験版|体验版|試読版?|试读版?|サンプル|\bsample\b|\bpreview\b|\btrial\b",
    re.IGNORECASE,
)
_PART_WORD_RE = re.compile(
    r"(?:"
    r"(?P<edge>前[編篇]|中[編篇]|後[編篇]|后[編篇]|完結[編篇]|完结[編篇]|[上中下][巻卷])|"
    r"(?:^|[\s\[【（(])(?P<standalone>[上中下])(?=$|[\s\]】）)])|"
    r"(?i:\b(?:part|vol\.?|act)\s*(?P<roman_labeled>I|II|III|IV|V|VI)\b)|"
    r"(?:^|[\s\[【（(])(?P<roman_multi>II|III|IV|VI)(?=$|[\s\]】）)])"
    r")"
)
_EDGE_CANONICAL = {
    "前編": "part:前篇",
    "前篇": "part:前篇",
    "中編": "part:中篇",
    "中篇": "part:中篇",
    "後編": "part:后篇",
    "後篇": "part:后篇",
    "后編": "part:后篇",
    "后篇": "part:后篇",
    "完結編": "part:完结篇",
    "完結篇": "part:完结篇",
    "完结編": "part:完结篇",
    "完结篇": "part:完结篇",
    "上巻": "vol:上",
    "上卷": "vol:上",
    "中巻": "vol:中",
    "中卷": "vol:中",
    "下巻": "vol:下",
    "下卷": "vol:下",
}
_KANJI_NUM_PART_RE = re.compile(
    r"第\s*([一二三四五六七八九十]+)\s*([部話话回巻卷編篇])"
)
_KANJI_MAP = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

# Curated 1-to-1 Traditional/Kyujitai -> Simplified CJK mapping for cross-script matching.
_TRAD_TO_SIMP_PAIRS: dict[str, str] = {
    "愛": "爱", "惡": "恶", "悪": "恶", "壓": "压", "奧": "奥", "罷": "罢", "擺": "摆",
    "敗": "败", "辦": "办", "幫": "帮", "綁": "绑", "寶": "宝", "報": "报", "貝": "贝",
    "備": "备", "輩": "辈", "筆": "笔", "幣": "币", "畢": "毕", "閉": "闭", "邊": "边",
    "辺": "边", "編": "编", "變": "变", "変": "变", "標": "标", "別": "别", "賓": "宾",
    "繽": "缤", "餅": "饼", "並": "并", "撥": "拨", "補": "补", "佈": "布", "猜": "猜",
    "財": "财", "採": "采", "參": "参", "殘": "残", "慘": "惨", "倉": "仓", "蒼": "苍",
    "藏": "藏", "冊": "册", "測": "测", "層": "层", "產": "产", "長": "长", "場": "场",
    "腸": "肠", "廠": "厂", "車": "车", "徹": "彻", "塵": "尘", "陳": "陈", "稱": "称",
    "懲": "惩", "誠": "诚", "癡": "痴", "遲": "迟", "遅": "迟", "齒": "齿", "歯": "齿",
    "衝": "冲", "蟲": "虫", "寵": "宠", "醜": "丑", "觸": "触", "處": "处", "処": "处",
    "傳": "传", "創": "创", "純": "纯", "詞": "词", "辭": "辞", "辞": "辞", "從": "从",
    "聰": "聪", "錯": "错", "達": "达", "帶": "带", "貸": "贷", "單": "单", "単": "单",
    "膽": "胆", "彈": "弹", "當": "当", "黨": "党", "導": "导", "島": "岛", "禱": "祷",
    "盜": "盗", "燈": "灯", "敵": "敌", "遞": "递", "點": "点", "電": "电", "墊": "垫",
    "調": "调", "訂": "订", "東": "东", "動": "动", "凍": "冻", "棟": "栋", "鬥": "斗",
    "闘": "斗", "讀": "读", "読": "读", "獨": "独", "賭": "赌", "斷": "断", "對": "对",
    "対": "对", "隊": "队", "頓": "顿", "奪": "夺", "墮": "堕", "兒": "儿", "爾": "尔",
    "發": "发", "髮": "发", "髪": "发", "罰": "罚", "飯": "饭", "範": "范", "販": "贩",
    "訪": "访", "飛": "飞", "費": "费", "紛": "纷", "墳": "坟", "奮": "奋", "豐": "丰",
    "豊": "丰", "風": "风", "瘋": "疯", "縫": "缝", "鳳": "凤", "膚": "肤", "複": "复",
    "負": "负", "婦": "妇", "縛": "缚", "該": "该", "蓋": "盖", "乾": "干", "趕": "赶",
    "剛": "刚", "鋼": "钢", "個": "个", "給": "给", "貢": "贡", "溝": "沟", "構": "构",
    "購": "购", "夠": "够", "顧": "顾", "關": "关", "関": "关", "觀": "观", "観": "观",
    "館": "馆", "慣": "惯", "廣": "广", "広": "广", "歸": "归", "帰": "归", "龜": "龟",
    "規": "规", "軌": "轨", "櫃": "柜", "貴": "贵", "國": "国", "過": "过", "還": "还",
    "漢": "汉", "號": "号", "賀": "贺", "紅": "红", "後": "后", "護": "护", "華": "华",
    "畫": "画", "話": "话", "懷": "怀", "壞": "坏", "歡": "欢", "環": "环", "換": "换",
    "黃": "黄", "會": "会", "繪": "绘", "絵": "绘", "獲": "获", "貨": "货", "機": "机",
    "積": "积", "極": "极", "擊": "击", "級": "级", "擠": "挤", "幾": "几", "記": "记",
    "紀": "纪", "繼": "继", "継": "继", "際": "际", "劑": "剂", "價": "价", "価": "价",
    "駕": "驾", "間": "间", "監": "监", "堅": "坚", "艱": "艰", "簡": "简", "見": "见",
    "劍": "剑", "剣": "剑", "鍵": "键", "將": "将", "獎": "奖", "講": "讲", "醬": "酱",
    "嬌": "娇", "膠": "胶", "階": "阶", "節": "节", "結": "结", "傑": "杰", "僅": "仅",
    "進": "进", "盡": "尽", "經": "经", "経": "经", "驚": "惊", "鏡": "镜", "競": "竞",
    "淨": "净", "舊": "旧", "舉": "举", "挙": "举", "據": "据", "劇": "剧", "懼": "惧",
    "捲": "卷", "覺": "觉", "絕": "绝", "絶": "绝", "軍": "军", "開": "开", "凱": "凯",
    "課": "课", "庫": "库", "誇": "夸", "塊": "块", "寬": "宽", "況": "况", "礦": "矿",
    "鉱": "矿", "虧": "亏", "擴": "扩", "來": "来", "賴": "赖", "頼": "赖", "藍": "蓝",
    "欄": "栏", "懶": "懒", "爛": "烂", "勞": "劳", "樂": "乐", "楽": "乐", "類": "类",
    "離": "离", "禮": "礼", "裡": "里", "裏": "里", "歷": "历", "曆": "历", "麗": "丽",
    "勵": "励", "連": "连", "聯": "联", "憐": "怜", "練": "练", "戀": "恋", "恋": "恋",
    "兩": "两", "療": "疗", "瞭": "了", "臨": "临", "靈": "灵", "霊": "灵", "領": "领",
    "劉": "刘", "龍": "龙", "竜": "龙", "樓": "楼", "盧": "卢", "陸": "陆", "錄": "录",
    "録": "录", "亂": "乱", "輪": "轮", "論": "论", "羅": "罗", "媽": "妈", "馬": "马",
    "碼": "码", "買": "买", "売": "卖", "賣": "卖", "滿": "满", "満": "满", "貓": "猫",
    "麼": "么", "沒": "没", "門": "门", "們": "们", "夢": "梦", "彌": "弥", "麵": "面",
    "滅": "灭", "鳴": "鸣", "銘": "铭", "謀": "谋", "難": "难", "腦": "脑", "脳": "脑",
    "內": "内", "擬": "拟", "鳥": "鸟", "寧": "宁", "濃": "浓", "農": "农", "歐": "欧",
    "盤": "盘", "龐": "庞", "賠": "赔", "噴": "喷", "騙": "骗", "飄": "飘", "頻": "频",
    "貧": "贫", "評": "评", "潑": "泼", "撲": "扑", "僕": "仆", "鋪": "铺", "樸": "朴",
    "齊": "齐", "騎": "骑", "啟": "启", "氣": "气", "気": "气", "棄": "弃", "遷": "迁",
    "簽": "签", "錢": "钱", "銭": "钱", "潛": "潜", "淺": "浅", "浅": "浅", "槍": "枪",
    "強": "强", "搶": "抢", "橋": "桥", "喬": "乔", "親": "亲", "輕": "轻", "軽": "轻",
    "傾": "倾", "晴": "晴", "請": "请", "慶": "庆", "窮": "穷", "區": "区", "驅": "驱",
    "駆": "驱", "趨": "趋", "權": "权", "権": "权", "勸": "劝", "確": "确", "讓": "让",
    "譲": "让", "熱": "热", "認": "认", "榮": "荣", "軟": "软", "銳": "锐", "潤": "润",
    "灑": "洒", "賽": "赛", "傘": "伞", "喪": "丧", "掃": "扫", "殺": "杀", "紗": "纱",
    "傷": "伤", "賞": "赏", "燒": "烧", "焼": "烧", "設": "设", "攝": "摄", "審": "审",
    "滲": "渗", "聲": "声", "声": "声", "勝": "胜", "聖": "圣", "師": "师", "獅": "狮",
    "濕": "湿", "詩": "诗", "時": "时", "實": "实", "実": "实", "識": "识", "勢": "势",
    "視": "视", "試": "试", "飾": "饰", "釋": "释", "適": "适", "獸": "兽", "獣": "兽",
    "書": "书", "輸": "输", "屬": "属", "術": "术", "樹": "树", "雙": "双", "誰": "谁",
    "稅": "税", "順": "顺", "說": "说", "絲": "丝", "鬆": "松", "訴": "诉", "雖": "虽",
    "隨": "随", "歳": "岁", "歲": "岁", "孫": "孙", "損": "损", "縮": "缩", "鎖": "锁",
    "臺": "台", "態": "态", "談": "谈", "嘆": "叹", "湯": "汤", "討": "讨", "騰": "腾",
    "體": "体", "條": "条", "貼": "贴", "鐵": "铁", "鉄": "铁", "聽": "听", "聴": "听",
    "廳": "厅", "統": "统", "頭": "头", "圖": "图", "図": "图", "塗": "涂", "團": "团",
    "脫": "脱", "襪": "袜", "彎": "弯", "灣": "湾", "萬": "万", "網": "网", "偽": "伪",
    "衛": "卫", "為": "为", "維": "维", "偉": "伟", "溫": "温", "聞": "闻", "問": "问",
    "臥": "卧", "烏": "乌", "無": "无", "吳": "吴", "霧": "雾", "習": "习", "戲": "戏",
    "細": "细", "蝦": "虾", "嚇": "吓", "鮮": "鲜", "纖": "纤", "閑": "闲", "賢": "贤",
    "鹹": "咸", "顯": "显", "險": "险", "現": "现", "獻": "献", "縣": "县", "線": "线",
    "鄉": "乡", "響": "响", "銷": "销", "曉": "晓", "寫": "写", "謝": "谢", "興": "兴",
    "許": "许", "續": "续", "続": "续", "選": "选", "學": "学", "尋": "寻", "訊": "讯",
    "訓": "训", "亞": "亚", "煙": "烟", "嚴": "严", "厳": "严", "顏": "颜", "顔": "颜",
    "厭": "厌", "驗": "验", "験": "验", "養": "养", "樣": "样", "様": "样", "遙": "遥",
    "藥": "药", "薬": "药", "爺": "爷", "葉": "叶", "頁": "页", "業": "业", "醫": "医",
    "儀": "仪", "遺": "遗", "義": "义", "藝": "艺", "芸": "艺", "議": "议", "異": "异",
    "譯": "译", "訳": "译", "陰": "阴", "銀": "银", "飲": "饮", "隱": "隐", "隠": "隐",
    "應": "应", "応": "应", "營": "营", "擁": "拥", "優": "优", "憂": "忧", "郵": "邮",
    "遊": "游", "猶": "犹", "誘": "诱", "於": "于", "魚": "鱼", "漁": "渔", "與": "与",
    "語": "语", "獄": "狱", "慾": "欲", "預": "预", "禦": "御", "圓": "圆", "園": "园",
    "員": "员", "緣": "缘", "縁": "缘", "遠": "远", "願": "愿", "約": "约", "躍": "跃",
    "雲": "云", "運": "运", "韻": "韵", "雜": "杂", "雑": "杂", "載": "载", "暫": "暂",
    "讚": "赞", "賛": "赞", "臟": "脏", "臓": "脏", "責": "责", "擇": "择", "則": "则",
    "澤": "泽", "沢": "泽", "贈": "赠", "戰": "战", "戦": "战", "張": "张", "帳": "帐",
    "漲": "涨", "這": "这", "貞": "贞", "針": "针", "陣": "阵", "鎮": "镇", "爭": "争",
    "徵": "征", "證": "证", "証": "证", "織": "织", "職": "职", "執": "执", "紙": "纸",
    "誌": "志", "製": "制", "質": "质", "鐘": "钟", "終": "终", "種": "种", "眾": "众",
    "週": "周", "晝": "昼", "昼": "昼", "豬": "猪", "諸": "诸", "築": "筑", "專": "专",
    "轉": "转", "転": "转", "裝": "装", "狀": "状", "準": "准", "資": "资", "總": "总",
    "総": "总", "縱": "纵", "縦": "纵", "組": "组", "鑽": "钻", "遵": "遵", "桜": "樱",
    "櫻": "樱", "駅": "驿", "驛": "驿", "黒": "黑", "嬢": "娘", "済": "济", "縄": "绳",
    "舎": "舍", "蔵": "藏", "薫": "熏", "頂": "顶", "齢": "龄", "姉": "姐", "姊": "姐",
}
_TRAD_TO_SIMP_TABLE = str.maketrans(_TRAD_TO_SIMP_PAIRS)


@dataclass(frozen=True, slots=True)
class BracketSpan:
    start: int
    end: int
    open_char: str
    content: str


@dataclass(frozen=True, slots=True)
class TitleIdentity:
    raw_title: str
    normalized_title: str
    identity_core: str
    context_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TitleSemantics:
    edition_kind: frozenset[str]
    part_markers: tuple[str, ...]

    def conflicts_with(self, other: "TitleSemantics") -> bool:
        if self.edition_kind != other.edition_kind:
            return True
        if self.part_markers != other.part_markers:
            return True
        return False


@dataclass(frozen=True, slots=True)
class PreparedIdentityText:
    normalized: str
    length: int
    ngrams: frozenset[str]
    tokens: frozenset[str]
    char_counts: tuple[tuple[str, int], ...] = ()


def fold_cjk_variants(value: str) -> str:
    """Fold common Traditional/Kyujitai CJK characters to Simplified equivalents."""
    return value.translate(_TRAD_TO_SIMP_TABLE)


def iter_top_level_brackets(value: str) -> list[BracketSpan]:
    """Scan balanced top-level bracket spans (supporting 1+ nested parens inside)."""
    spans: list[BracketSpan] = []
    n = len(value)
    idx = 0
    while idx < n:
        ch = value[idx]
        group = _OPEN_TO_GROUP.get(ch)
        if group is None:
            idx += 1
            continue
        stack = [(group, idx)]
        pos = idx + 1
        matched = False
        while pos < n and pos - idx <= _MAX_BRACKET_SPAN:
            cur = value[pos]
            open_grp = _OPEN_TO_GROUP.get(cur)
            if open_grp is not None:
                stack.append((open_grp, pos))
            else:
                close_grp = _CLOSE_TO_GROUP.get(cur)
                if close_grp is not None and stack[-1][0] == close_grp:
                    _, start = stack.pop()
                    if not stack:
                        content = value[start + 1 : pos]
                        if content.strip():
                            spans.append(
                                BracketSpan(
                                    start=start,
                                    end=pos + 1,
                                    open_char=value[start],
                                    content=content,
                                )
                            )
                        idx = pos + 1
                        matched = True
                        break
            pos += 1
        if not matched:
            idx += 1
    return spans


def _compact(value: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", value).casefold())


def _author_variants(author_names: str | Iterable[str]) -> tuple[list[str], set[str]]:
    if isinstance(author_names, str):
        raw_items = [author_names] if author_names else []
    else:
        raw_items = [item for item in author_names if item]
    ordered: list[str] = []
    compacts: set[str] = set()
    seen: set[str] = set()
    for raw in raw_items:
        folded = unicodedata.normalize("NFKC", html.unescape(raw)).casefold().strip()
        if not folded:
            continue
        compact = _compact(folded)
        if compact:
            compacts.add(compact)
        if folded not in seen:
            seen.add(folded)
            ordered.append(folded)
    ordered.sort(key=len, reverse=True)
    return ordered, compacts


def _is_version_content(content: str) -> bool:
    if any(re.search(pattern, content, re.IGNORECASE) for pattern, _ in VERSION_PATTERNS):
        return True
    if _STATUS_NOISE_RE.search(content):
        return True
    if _BRACKET_VERSION_RE.search(content.strip()):
        return True
    return bool(_CHAPTER_RANGE_RE.fullmatch(content.strip()))


def _is_creator_content(content: str, author_compacts: set[str]) -> bool:
    compact_content = _compact(content)
    if author_compacts and any(
        author_c in compact_content for author_c in author_compacts if len(author_c) >= 2
    ):
        return True
    if author_compacts and compact_content in author_compacts:
        return True
    if re.search(r"works|circle|サークル", content, re.IGNORECASE):
        return True
    return "(" in content or "（" in content


def normalize_title(title: str, author_name: str | Iterable[str] = "") -> str:
    """Strip event, creator, and upload noise while preserving core work identity."""
    value = unicodedata.normalize("NFKC", html.unescape(title)).casefold()
    value = re.sub(r"[×✕✖]", " x ", value)
    for _ in range(4):
        updated = _EVENT_PREFIX_RE.sub("", value)
        if updated == value:
            break
        value = updated

    author_list, author_compacts = _author_variants(author_name)
    spans = iter_top_level_brackets(value)
    if spans:
        parts: list[str] = []
        cursor = 0
        leading_end = 1
        for span in spans:
            if span.start > cursor:
                prefix_gap = value[cursor : span.start]
                if prefix_gap.strip():
                    leading_end = -1
                parts.append(prefix_gap)
            is_leading = leading_end >= 0 and span.start <= leading_end
            is_version = _is_version_content(span.content)
            is_creator = is_leading and _is_creator_content(span.content, author_compacts)
            if is_version or is_creator:
                parts.append(" ")
                if is_leading:
                    leading_end = span.end + 1
            else:
                parts.append(f" {span.content} ")
                leading_end = -1
            cursor = span.end
        if cursor < len(value):
            parts.append(value[cursor:])
        value = "".join(parts)

    value = _CHAPTER_RANGE_RE.sub(" ", value)
    value = _SINGLE_CHAPTER_RE.sub(
        lambda m: f" ch {m.group(1) or m.group(2) or m.group(3)} ", value
    )
    value = _STATUS_NOISE_RE.sub(" ", value)
    for pattern, _ in VERSION_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)

    for name in author_list:
        compact_name = _compact(name)
        if len(compact_name) < 2:
            continue
        if re.fullmatch(r"[a-z0-9_ ]+", name):
            if len(compact_name) < 3:
                continue
            value = re.sub(
                rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])",
                " ",
                value,
                flags=re.IGNORECASE,
            )
        else:
            value = re.sub(re.escape(name), " ", value, flags=re.IGNORECASE)

    value = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", " ", value)
    return " ".join(value.split()).strip()


def extract_title_semantics(raw_title: str) -> TitleSemantics:
    """Extract non-mergeable work kind and part/volume semantics from raw title."""
    value = unicodedata.normalize("NFKC", html.unescape(raw_title))
    for _ in range(4):
        updated = _EVENT_PREFIX_RE.sub("", value)
        if updated == value:
            break
        value = updated

    spans = iter_top_level_brackets(value)
    leading_end = 1
    body_parts: list[str] = []
    bracket_texts: list[str] = []
    cursor = 0
    for span in spans:
        if span.start > cursor:
            gap = value[cursor : span.start]
            if gap.strip():
                leading_end = -1
            body_parts.append(gap)
        is_leading = leading_end >= 0 and span.start <= leading_end
        if is_leading and _is_creator_content(span.content, set()):
            leading_end = span.end + 1
        elif is_leading and _is_version_content(span.content):
            leading_end = span.end + 1
        else:
            leading_end = -1
            bracket_texts.append(span.content)
            body_parts.append(f" {span.content} ")
        cursor = span.end
    if cursor < len(value):
        body_parts.append(value[cursor:])
    cleaned_cased = " ".join("".join(body_parts).split())

    kinds: set[str] = set()
    if _COMPILATION_RE.search(cleaned_cased) or any(
        _COMPILATION_BRACKET_RE.search(text) for text in bracket_texts
    ):
        kinds.add("compilation")
    if _SEQUEL_RE.search(cleaned_cased):
        kinds.add("sequel")
    if _SPINOFF_RE.search(cleaned_cased):
        kinds.add("spinoff")
    if _SAMPLE_RE.search(cleaned_cased):
        kinds.add("sample")

    parts: list[str] = []
    for match in _PART_WORD_RE.finditer(cleaned_cased):
        if match.group("edge"):
            token = match.group("edge")
            parts.append(_EDGE_CANONICAL.get(token, f"edge:{fold_cjk_variants(token)}"))
        elif match.group("standalone"):
            parts.append(f"vol:{match.group('standalone')}")
        elif match.group("roman_labeled"):
            parts.append(f"roman:{match.group('roman_labeled').casefold()}")
        elif match.group("roman_multi"):
            parts.append(f"roman:{match.group('roman_multi').casefold()}")

    for match in _KANJI_NUM_PART_RE.finditer(cleaned_cased):
        kanji = match.group(1)
        unit = fold_cjk_variants(match.group(2))
        if unit == "編":
            unit = "篇"
        if len(kanji) == 1 and kanji in _KANJI_MAP:
            parts.append(f"knum:{_KANJI_MAP[kanji]}:{unit}")
        elif kanji in {"十一", "十二"}:
            parts.append(f"knum:{10 + _KANJI_MAP[kanji[1]]}:{unit}")

    return TitleSemantics(
        edition_kind=frozenset(kinds),
        part_markers=tuple(dict.fromkeys(parts)),
    )


def prepare_identity_text(value: str) -> PreparedIdentityText:
    normalized = normalize_identity_text(value)
    return PreparedIdentityText(
        normalized=normalized,
        length=len(normalized),
        ngrams=frozenset(_ngrams(normalized)),
        tokens=frozenset(normalized.split()),
        char_counts=tuple(Counter(normalized).items()),
    )


def normalize_identity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    normalized = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", " ", normalized)
    return " ".join(normalized.split()).strip()


def parse_title_identity(raw_title: str, normalized_title: str) -> TitleIdentity:
    contexts: list[str] = []
    unfolded = unicodedata.normalize("NFKC", html.unescape(raw_title))
    leading_end = 1
    for span in iter_top_level_brackets(unfolded):
        content = " ".join(span.content.split()).strip()
        normalized_content = normalize_identity_text(content)
        is_leading = span.start <= leading_end
        if is_leading and (span.open_char in {"[", "【"} or _EVENT_ONLY_RE.match(content)):
            leading_end = span.end + 1
            continue
        if (
            not normalized_content
            or _is_version_content(content)
            or _EVENT_ONLY_RE.match(content)
            or _NON_CONTEXT_BRACKET_RE.match(content)
        ):
            if is_leading:
                leading_end = span.end + 1
            continue
        leading_end = -1
        contexts.append(normalized_content)

    core = normalize_identity_text(normalized_title)
    for context in sorted(set(contexts), key=len, reverse=True):
        core = re.sub(
            rf"(?<!\w){re.escape(context)}(?!\w)", " ", core, flags=re.IGNORECASE
        )
    core = " ".join(core.split()).strip() or normalize_identity_text(normalized_title)
    return TitleIdentity(
        raw_title=raw_title,
        normalized_title=normalize_identity_text(normalized_title),
        identity_core=core,
        context_terms=tuple(dict.fromkeys(contexts)),
    )


def identity_names(
    raw_title: str, normalized_title: str, aliases: list[str] | tuple[str, ...]
) -> set[str]:
    names = {parse_title_identity(raw_title, normalized_title).identity_core}
    names.update(
        parse_title_identity(alias, alias).identity_core for alias in aliases if alias
    )
    return {name for name in names if name}


def weighted_text_similarity(left: str, right: str) -> float:
    left_value = normalize_identity_text(left)
    right_value = normalize_identity_text(right)
    if not left_value or not right_value:
        return 0.0
    if left_value == right_value:
        return 1.0
    sequence_score = SequenceMatcher(None, left_value, right_value).ratio()
    ngram_score = _dice(_ngrams(left_value), _ngrams(right_value))
    token_score = _dice(set(left_value.split()), set(right_value.split()))
    structural_score = max(ngram_score, token_score)
    return round(sequence_score * 0.62 + structural_score * 0.38, 5)


def best_identity_similarity(left_names: set[str], right_names: set[str]) -> float:
    return max(
        (
            weighted_text_similarity(left, right)
            for left in left_names
            for right in right_names
        ),
        default=0.0,
    )


def best_prepared_similarity(
    left_items: tuple[PreparedIdentityText, ...],
    right_items: tuple[PreparedIdentityText, ...],
    *,
    min_score: float = 0.0,
) -> float:
    best = 0.0
    cutoff = min_score
    for left in left_items:
        if not left.length:
            continue
        for right in right_items:
            if not right.length:
                continue
            if left.normalized == right.normalized:
                return 1.0
            ngram_score = _dice(left.ngrams, right.ngrams)
            token_score = _dice(left.tokens, right.tokens)
            structural_score = ngram_score if ngram_score >= token_score else token_score
            shorter = left.length if left.length <= right.length else right.length
            total_len = left.length + right.length
            len_bound = (2.0 * shorter) / total_len
            if len_bound * 0.62 + structural_score * 0.38 + 1e-6 <= cutoff:
                continue
            if left.char_counts and right.char_counts and cutoff > 0.0:
                right_map = dict(right.char_counts)
                lcs_upper = 0
                for ch, cnt in left.char_counts:
                    rc = right_map.get(ch)
                    if rc:
                        lcs_upper += cnt if cnt <= rc else rc
                char_bound = (2.0 * lcs_upper) / total_len
                if char_bound * 0.62 + structural_score * 0.38 + 1e-6 <= cutoff:
                    continue
            sequence_score = SequenceMatcher(None, left.normalized, right.normalized).ratio()
            score = round(sequence_score * 0.62 + structural_score * 0.38, 5)
            if score > best:
                best = score
                if best > cutoff:
                    cutoff = best
    return best


def shared_context(left: TitleIdentity, right: TitleIdentity) -> list[str]:
    return sorted(set(left.context_terms) & set(right.context_terms))


def _ngrams(value: str, size: int = 2) -> set[str]:
    compact = re.sub(r"\s+", "", value)
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _dice(left: set[str] | frozenset[str], right: set[str] | frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))
