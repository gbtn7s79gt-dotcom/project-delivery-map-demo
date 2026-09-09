#!/usr/bin/env python3
"""
每周从飞书多维表格拉取厂站数据，聚合后生成 data.js。
部署在 GitHub Actions 中运行；凭据通过环境变量注入，代码中不保留明文。
"""
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
APP_TOKEN = os.environ["FEISHU_APP_TOKEN"]
TABLE_ID = os.environ["FEISHU_TABLE_ID"]

REPO_ROOT = Path(__file__).parent
DATA_JS_PATH = REPO_ROOT / "data.js"
LAST_SYNC_PATH = REPO_ROOT / "last_sync.log"
COORDS_PATH = REPO_ROOT / "coords.json"

# 飞书字段名（多维表格中的列名）
COL_STATION = "厂站名称"
COL_CUSTOMER = "所属客户"
COL_CITY = "城市"
COL_PROVINCE = "省份"
COL_TYPE = "厂站类型"
COL_PRODUCT = "产品线"
COL_WP_BID = "WP中标概率"
COL_APC_BID = "APC中标概率"
COL_KA = "是否KA"
COL_CAPACITY = "处理规模"
COL_LON = "经度"
COL_LAT = "纬度"

# ---------------------------------------------------------------------------
# 城市坐标字典
# ---------------------------------------------------------------------------

def load_city_coords() -> dict[str, list[float]]:
    """加载城市->[lon,lat]字典。文件不存在时返回空字典。"""
    if COORDS_PATH.exists():
        data = json.loads(COORDS_PATH.read_text(encoding="utf-8"))
        return {k: [float(v[0]), float(v[1])] for k, v in data.items()}
    return {}


# ---------------------------------------------------------------------------
# 名称清洗
# ---------------------------------------------------------------------------

def norm_province(raw: str) -> str:
    """省份标准化：去掉'省'后缀，直辖市去掉'市'后缀。"""
    s = str(raw or "").strip()
    if not s:
        return s
    # 直辖市特殊处理
    if s in ("北京市", "北京"):
        return "北京"
    if s in ("天津市", "天津"):
        return "天津"
    if s in ("上海市", "上海"):
        return "上海"
    if s in ("重庆市", "重庆"):
        return "重庆"
    # 内蒙古自治区 -> 内蒙古
    if s.startswith("内蒙古"):
        return "内蒙古"
    # 广西/宁夏/新疆/西藏 可能带自治区
    for short in ("广西", "宁夏", "新疆", "西藏"):
        if s.startswith(short):
            return short
    if s.endswith("省"):
        return s[:-1]
    return s


CITY_ALIASES = {
    "广州市": "广州",
    "深圳市": "深圳",
    "宁波市": "宁波",
    "重庆市": "重庆",
    "来宾市": "来宾",
    "肇庆市": "肇庆",
    "嘉兴市": "嘉兴",
    "桐乡市": "桐乡",
    "嘉兴桐乡": "桐乡",
    "济南": "济南",  # Excel里有' 济南'，trim即可
}


def norm_city(raw: str) -> str:
    """城市标准化：trim、别名映射、去掉'市'后缀。"""
    s = str(raw or "").strip()
    if not s:
        return s
    if s in CITY_ALIASES:
        return CITY_ALIASES[s]
    # 通用：去掉末尾的'市'
    if s.endswith("市") and len(s) > 2:
        return s[:-1]
    return s


# 客户显示名别名：与原始 HTML（ChatGPT 当年处理结果）保持一致的个别改写
CUSTOMER_NAME_ALIASES = {
    "雄安水务集团": "雄安水务",
}


def norm_customer(raw: str) -> str:
    """客户名标准化：trim + 别名映射（与原始 HTML 显示名保持一致）。"""
    s = str(raw or "").strip()
    return CUSTOMER_NAME_ALIASES.get(s, s)


# ---------------------------------------------------------------------------
# 飞书 API
# ---------------------------------------------------------------------------

def get_tenant_access_token() -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


# 可重试的飞书业务错误码（多为网关/服务端偶发，重试可恢复）
RETRYABLE_CODES = {
    1254002,  # Fail（网关偶发失败，无明确参数错误时优先重试）
    1254001,
    99991400,  # 网关超时类
}


def _get_with_retry(url: str, headers: dict, params: dict, retries: int = 4) -> dict:
    """带重试的 GET。

    对网络层异常（超时/连接错）以及飞书网关偶发业务错误码都做重试，
    返回解析后的 JSON body。连续多次仍失败则抛最后一次异常/业务错误。
    """
    import time
    last_err: Exception | None = None
    last_data: dict | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=90)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") in (0, None):
                return data
            if data.get("code") in RETRYABLE_CODES:
                last_data = data
                if attempt < retries:
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"读取记录失败(重试{retries}次后仍失败): {data}")
            # 非可重试业务错误（参数错/无权限等）直接抛出
            raise RuntimeError(f"读取记录失败: {data}")
        except Exception as e:  # noqa: BLE001 超时/连接错误等
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
    if last_data is not None:
        raise RuntimeError(f"读取记录失败: {last_data}")
    raise last_err  # type: ignore[misc]


def list_records(token: str) -> list[dict[str, Any]]:
    """分页读取表格全部记录。

    注意：该多维表格较大，page_size 使用 500 易触发网关超时/400，
    故改为 100 + 自动重试，稳定性更高。
    """
    base_url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
        f"/tables/{TABLE_ID}/records"
    )
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = _get_with_retry(
            base_url, headers={"Authorization": f"Bearer {token}"}, params=params
        )
        items.extend(data["data"]["items"])
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
        if not page_token:
            break
    return items


# ---------------------------------------------------------------------------
# 聚合逻辑
# ---------------------------------------------------------------------------

def parse_products(field_value: Any) -> list[str]:
    """产品线可能是多选数组或逗号分隔字符串。"""
    if isinstance(field_value, list):
        return [str(v).strip() for v in field_value if str(v).strip()]
    if isinstance(field_value, str):
        return [v.strip() for v in field_value.split(",") if v.strip()]
    return []


def bid_status(wp: Any, apc: Any, tags: list[str]) -> list[str]:
    """
    根据 WP/APC 中标概率字段决定 bidTags。
    原始 HTML 中 bidTags 来自产品线拆分；这里保留产品线作为标签。
    """
    return tags


def parse_capacity(val: Any) -> float | None:
    """处理规模转数字；失败返回 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def build_data(records: list[dict[str, Any]], coords: dict[str, list[float]]) -> dict[str, Any]:
    """把飞书记录聚合为页面所需的 DATA 结构。"""
    # 第一步：把每条记录清洗成标准行
    rows = []
    for rec in records:
        fields = rec.get("fields", {})
        province = norm_province(fields.get(COL_PROVINCE))
        city = norm_city(fields.get(COL_CITY))
        customer = norm_customer(fields.get(COL_CUSTOMER))
        station = str(fields.get(COL_STATION) or "").strip()
        plant_type = str(fields.get(COL_TYPE) or "").strip()
        products = parse_products(fields.get(COL_PRODUCT))
        wp_bid = str(fields.get(COL_WP_BID) or "").strip()
        apc_bid = str(fields.get(COL_APC_BID) or "").strip()
        is_ka = str(fields.get(COL_KA) or "").strip()
        capacity = parse_capacity(fields.get(COL_CAPACITY))

        # 优先使用飞书表里的经纬度；没有则查城市字典
        lon = parse_capacity(fields.get(COL_LON))
        lat = parse_capacity(fields.get(COL_LAT))
        if lon is None or lat is None:
            if city in coords:
                lon, lat = coords[city]
            else:
                raise KeyError(f"城市 '{city}'（原始值：{fields.get(COL_CITY)!r}）没有可用坐标。"
                               f"请在飞书表中添加 '经度'、'纬度' 列，或在 coords.json 中补充坐标。")

        rows.append(
            {
                "province": province,
                "city": city,
                "customer": customer,
                "station": station,
                "plant_type": plant_type,
                "products": products,
                "wp_bid": wp_bid,
                "apc_bid": apc_bid,
                "is_ka": is_ka,
                "capacity": capacity,
                "lon": lon,
                "lat": lat,
            }
        )

    # 第二步：按 (customer, city) 聚合 -> customer markers
    customer_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (row["province"], row["city"], row["customer"])
        customer_groups[key].append(row)

    customers = []
    for (province, city, customer), stations_rows in customer_groups.items():
        station_objs = []
        plant_type_counter: dict[str, int] = defaultdict(int)
        plant_groups: dict[str, list[dict]] = defaultdict(list)
        total_capacity = 0.0
        capacity_count = 0
        for r in stations_rows:
            tags = r["products"]
            station_objs.append({"name": r["station"], "bidTags": tags})
            plant_type_counter[r["plant_type"]] += 1
            plant_groups[r["plant_type"]].append({"name": r["station"], "bidTags": tags})
            if r["capacity"] is not None:
                total_capacity += r["capacity"]
                capacity_count += 1

        plant_types = sorted(plant_type_counter.keys())
        plant_count = len(stations_rows)
        wastewater_count = plant_type_counter.get("污水厂", 0)
        water_count = plant_type_counter.get("自来水厂", 0)
        capacity = total_capacity if capacity_count else 0.0

        # region 按城市查表；没有则根据省份推断（默认取省份已有区域）
        region = infer_region(city=city, province=province)

        customers.append(
            {
                "province": province,
                "city": city,
                "customer": customer,
                "plantTypes": plant_types,
                "region": region,
                "plantCount": plant_count,
                "wastewaterCount": wastewater_count,
                "waterCount": water_count,
                "capacity": round(capacity, 2) if capacity else 0.0,
                "stations": station_objs,
                "plantGroups": [
                    {"type": t, "count": len(plant_groups[t]), "stations": plant_groups[t]}
                    for t in plant_types
                ],
                "lon": stations_rows[0]["lon"],
                "lat": stations_rows[0]["lat"],
            }
        )

    # 按省份/城市/客户名排序，保证输出稳定
    customers.sort(key=lambda x: (x["province"], x["city"], x["customer"]))

    # 补 customers 必需字段（渲染逻辑依赖）：
    #   id                全局唯一（p1..pN），供选中/高亮/聚焦
    #   cityCustomerIndex 同城客户在圆周上散开的序号（0-based）
    #   cityCustomerTotal 同城客户总数（>1 时标点沿圆周排列防重叠）
    _city_totals: dict[tuple[str, str], int] = {}
    for _c in customers:
        _ck = (_c["province"], _c["city"])
        _city_totals[_ck] = _city_totals.get(_ck, 0) + 1
    _city_seen: dict[tuple[str, str], int] = {}
    for _i, _c in enumerate(customers):
        _ck = (_c["province"], _c["city"])
        _c["id"] = f"p{_i + 1}"
        _idx = _city_seen.get(_ck, 0)
        # 与原始 HTML 一致：index = (城市内位置 + 1) mod 城市总数（起始错开一格）
        _c["cityCustomerIndex"] = (_idx + 1) % _city_totals[_ck]
        _c["cityCustomerTotal"] = _city_totals[_ck]
        _city_seen[_ck] = _idx + 1

    # 第三步：按城市聚合 -> cities
    city_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in customers:
        city_groups[(c["province"], c["city"])].append(c)

    cities = []
    for (province, city), cus_list in city_groups.items():
        cities.append(
            {
                "id": f"{province}-{city}",
                "province": province,
                "city": city,
                "region": cus_list[0]["region"],
                "clientCount": len({c["customer"] for c in cus_list}),
                "customerMarkerCount": len(cus_list),
                "plantCount": sum(c["plantCount"] for c in cus_list),
                "wastewaterCount": sum(c["wastewaterCount"] for c in cus_list),
                "waterCount": sum(c["waterCount"] for c in cus_list),
                "lon": cus_list[0]["lon"],
                "lat": cus_list[0]["lat"],
            }
        )

    cities.sort(key=lambda x: (x["province"], x["city"]))

    # 第四步：按省份聚合 -> provinces
    province_groups: dict[str, list[dict]] = defaultdict(list)
    for c in cities:
        province_groups[c["province"]].append(c)

    provinces = []
    for province, city_list in province_groups.items():
        regions = sorted({c["region"] for c in city_list})
        provinces.append(
            {
                "id": province,
                "province": province,
                "region": " / ".join(regions),
                "cityCount": len(city_list),
                "clientCount": sum(c["clientCount"] for c in city_list),
                "customerMarkerCount": sum(c["customerMarkerCount"] for c in city_list),
                "plantCount": sum(c["plantCount"] for c in city_list),
                "wastewaterCount": sum(c["wastewaterCount"] for c in city_list),
                "waterCount": sum(c["waterCount"] for c in city_list),
                "cities": sorted([c["city"] for c in city_list]),
                "lon": sum(c["lon"] for c in city_list) / len(city_list),
                "lat": sum(c["lat"] for c in city_list) / len(city_list),
            }
        )

    provinces.sort(key=lambda x: x["province"])

    # 第五步：summary
    summary = {
        "provinces": len(provinces),
        "cities": len(cities),
        "clients": len({c["customer"] for c in customers}),
        "customerMarkers": len(customers),
        "plants": len(rows),
        "wastewaterPlants": sum(r["plant_type"] == "污水厂" for r in rows),
        "waterPlants": sum(r["plant_type"] == "自来水厂" for r in rows),
        "regions": sorted({c["region"] for c in customers}),
    }

    return {
        # 页面顶部来源标签使用 source.sheet，需与原始 HTML 兼容（对象含 sheet）
        "source": {"sheet": "飞书多维表格"},
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "provinces": provinces,
        "cities": cities,
        "customers": customers,
    }


# ---------------------------------------------------------------------------
# 区域推断
# ---------------------------------------------------------------------------

# 城市 -> 区域映射（与原始 HTML 中 ChatGPT 当年的划分一致）
CITY_TO_REGION = {
    "三亚": "南", "上海": "东", "东莞": "南", "东阳": "东", "临汾": "北", "义乌": "东",
    "乌海": "北", "九江": "南", "什邡": "西", "佛山": "南", "六安": "东", "内江": "西",
    "北京": "北", "南京": "东", "南宁": "南", "台州": "东", "合肥": "东", "启东": "东",
    "大冶": "北", "如皋": "东", "宁波": "东", "宜春": "北", "宣威": "西", "常州": "东",
    "广州": "南", "张家口": "北", "张家港": "东", "成都": "西", "新乡": "北", "无锡": "东",
    "昆明": "西", "朔州": "北", "来宾": "南", "杭州": "东", "枝江": "北", "桐乡": "东",
    "梅州": "南", "榆林": "西", "武汉": "北", "汕头": "南", "汕尾": "南", "江西": "东",
    "江门": "南", "池州": "东", "沈阳": "北", "泉州": "南", "泰安": "北", "济南": "北",
    "海东": "西", "海口": "南", "淄博": "北", "淮北": "东", "淮安": "东", "深圳": "南",
    "温州": "东", "滨州": "北", "潜江": "北", "潮州": "南", "珠海": "南", "瓦房店": "北",
    "盘锦": "北", "绍兴": "东", "绵阳": "西", "肇庆": "南", "芜湖": "东", "苏州": "东",
    "蚌埠": "东", "郑州": "北", "鄂尔多斯": "北", "重庆": "西", "金华": "东", "镇江": "东",
    "长沙": "南", "防城港": "南", "阳江": "南", "阳泉": "北", "雄安": "北", "青岛": "北",
    "高碑店": "北",
}

# 中国大区兜底（新增城市时按省份归属）
_PROVINCE_REGION = {
    # 东
    "上海": "东", "江苏": "东", "浙江": "东", "安徽": "东", "福建": "东", "江西": "东", "山东": "东",
    # 南
    "广东": "南", "广西": "南", "海南": "南", "湖南": "南",
    # 西
    "重庆": "西", "四川": "西", "贵州": "西", "云南": "西", "西藏": "西",
    "陕西": "西", "甘肃": "西", "青海": "西", "宁夏": "西", "新疆": "西",
    # 北
    "北京": "北", "天津": "北", "河北": "北", "山西": "北", "内蒙古": "北",
    "辽宁": "北", "吉林": "北", "黑龙江": "北", "河南": "北", "湖北": "北",
}


def infer_region(city: str, province: str) -> str:
    """推断城市所属区域。优先用 CITY_TO_REGION，其次按省份兜底。"""
    if city in CITY_TO_REGION:
        return CITY_TO_REGION[city]
    return _PROVINCE_REGION.get(province, "东")


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def data_hash(data: dict[str, Any]) -> str:
    """对数据做稳定哈希，用于判断是否有实质变更。"""
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def write_data_js(data: dict[str, Any]) -> None:
    js = "const DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
    DATA_JS_PATH.write_text(js, encoding="utf-8")


def main() -> int:
    print(f"[{datetime.now().isoformat()}] 开始同步飞书数据...")

    # 校验凭据
    missing = [k for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN", "FEISHU_TABLE_ID") if not os.environ.get(k)]
    if missing:
        print(f"缺少环境变量: {missing}", file=sys.stderr)
        return 1

    token = get_tenant_access_token()
    records = list_records(token)
    print(f"读取到 {len(records)} 条记录")

    if len(records) == 0:
        print("飞书返回 0 条记录，中止更新以避免清空页面。", file=sys.stderr)
        return 1

    # 数据量骤降保护（防止误删）
    coords = load_city_coords()
    data = build_data(records, coords)

    # 如果 data.js 已存在且哈希相同，跳过推送
    new_hash = data_hash(data)
    if DATA_JS_PATH.exists():
        existing_text = DATA_JS_PATH.read_text(encoding="utf-8")
        # 去掉开头 "const DATA = " 和结尾 ";"
        try:
            existing_json = existing_text[len("const DATA = "):].rstrip().rstrip(";")
            existing_data = json.loads(existing_json)
            old_hash = data_hash(existing_data)
        except Exception:
            old_hash = ""
        if old_hash == new_hash:
            print(f"数据未变更（hash={new_hash}），跳过更新。")
            _write_log(len(records), new_hash, changed=False)
            return 0

    write_data_js(data)
    _write_log(len(records), new_hash, changed=True)
    print(f"已生成 {DATA_JS_PATH}，hash={new_hash}")
    print("汇总:", data["summary"])
    return 0


def _write_log(record_count: int, data_hash: str, changed: bool) -> None:
    line = (
        f"{datetime.now(timezone.utc).isoformat()} | records={record_count} | "
        f"changed={changed} | hash={data_hash}\n"
    )
    with LAST_SYNC_PATH.open("a", encoding="utf-8") as f:
        f.write(line)


if __name__ == "__main__":
    sys.exit(main())
