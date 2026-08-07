"""高德地图服务封装 (httpx 直调高德 REST API)"""

import logging
import time

import httpx
from typing import List, Dict, Any, Optional
from ..config import get_settings
from ..models.schemas import Location, POIInfo, WeatherInfo

logger = logging.getLogger(__name__)

# 高德 REST API 基础地址
AMAP_BASE_URL = "https://restapi.amap.com"


class AmapService:
    """高德地图服务封装类

    通过高德开放平台 Web 服务 API 提供: POI搜索、天气查询、路线规划、地理编码、POI详情。
    相比原来基于 MCP 的实现, 直调 REST API 无需启动外部 MCP 服务进程, 自包含、易调试。
    """

    def __init__(self):
        """初始化服务"""
        settings = get_settings()
        self.api_key = settings.amap_api_key
        if not self.api_key:
            raise ValueError("高德地图API Key未配置,请在.env文件中设置AMAP_API_KEY")

        self.client = httpx.Client(timeout=10)
        # 图片URL内存缓存: name -> (url, expire_ts), 避免重复调用高德消耗配额/QPS
        self._photo_cache: Dict[str, tuple] = {}
        # QPS熔断时间戳: 触发CUQPS超限后, 该时间之前不再调用高德图片接口
        self._photo_blocked_until = 0.0

    def _get(self, path: str, params: Dict[str, Any]) -> dict:
        """GET 请求高德 API, 统一注入 key 并校验响应

        Args:
            path: API路径, 如 /v3/place/text
            params: 查询参数

        Returns:
            高德响应JSON(dict)

        Raises:
            ValueError: 高德返回 status != 1 时
        """
        request_params = {**params, "key": self.api_key}
        resp = self.client.get(f"{AMAP_BASE_URL}{path}", params=request_params)
        resp.raise_for_status()

        data = resp.json()
        if data.get("status") != "1":
            raise ValueError(f"高德API错误: {data.get('info', '未知错误')}")
        return data

    @staticmethod   # 静态方法装饰器，无需实例化即可调用，不需要读取或修改类里面的任何属性（所以连 self 参数都不需要传）
    def _parse_location(location: str) -> Location:
        """解析高德坐标字符串 "经度,纬度" 为 Location"""
        lon, lat = location.split(",")
        return Location(longitude=float(lon), latitude=float(lat))

    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        """搜索POI (兴趣点)

        Args:
            keywords: 搜索关键词, 如 "故宫" / "酒店"
            city: 城市, 如 "北京"
            citylimit: 是否仅搜索城市范围内

        Returns:
            POI信息列表
        """
        data = self._get("/v3/place/text", {
            "keywords": keywords,
            "city": city,
            "citylimit": str(citylimit).lower(),
            "offset": 10,
            "page": 1,
            "extensions": "all",
        })

        pois = []
        for item in data.get("pois", []):
            location = item.get("location", "")
            pois.append(POIInfo(
                id=item.get("id", ""),
                name=item.get("name", ""),
                type=item.get("type", ""),
                address=item.get("address", ""),
                location=self._parse_location(location) if location else Location(longitude=0, latitude=0),
                tel=item.get("tel") or None,
            ))
        return pois

    def get_weather(self, city: str) -> List[WeatherInfo]:
        """查询天气 (未来4天预报)

        高德天气接口需要城市编码(adcode), 因此先地理编码城市名获取 adcode。

        Args:
            city: 城市名称, 如 "北京"

        Returns:
            天气信息列表
        """
        # 1. 地理编码获取城市 adcode
        geocodes = self.geocode(city)
        if not geocodes:
            return []
        adcode = geocodes[0].get("adcode", "")

        # 2. 查询天气预报
        data = self._get("/v3/weather/weatherInfo", {
            "city": adcode,
            "extensions": "all",
        })

        weather_list = []
        for forecast in data.get("forecasts", []):
            for cast in forecast.get("casts", []):
                weather_list.append(WeatherInfo(
                    date=cast.get("date", ""),
                    day_weather=cast.get("dayweather", ""),
                    night_weather=cast.get("nightweather", ""),
                    day_temp=cast.get("daytemp", 0),
                    night_temp=cast.get("nighttemp", 0),
                    wind_direction=cast.get("daywind", ""),
                    wind_power=cast.get("daypower", ""),
                ))
        return weather_list

    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking",
    ) -> Dict[str, Any]:
        """规划路线

        高德路线接口需要经纬度, 因此先对起终点地址做地理编码。

        Args:
            origin_address: 起点地址
            destination_address: 终点地址
            origin_city: 起点城市 (可提高地理编码精度)
            destination_city: 终点城市
            route_type: 路线类型 (walking/driving/transit)

        Returns:
            路线信息 (distance/duration/route_type/description)
        """
        # 1. 地理编码起终点
        origin_geocodes = self.geocode(origin_address, origin_city)
        dest_geocodes = self.geocode(destination_address, destination_city)
        if not origin_geocodes or not dest_geocodes:
            return {}
        origin = origin_geocodes[0].get("location", "")
        destination = dest_geocodes[0].get("location", "")
        if not origin or not destination:
            return {}

        # 2. 根据路线类型选择接口
        if route_type == "driving":
            path = "/v3/direction/driving"
        elif route_type == "transit":
            path = "/v3/direction/transit/integrated"
        else:
            path = "/v3/direction/walking"

        data = self._get(path, {
            "origin": origin,
            "destination": destination,
        })

        paths = data.get("route", {}).get("paths", [])
        if not paths:
            return {}
        first = paths[0]
        distance = float(first.get("distance", 0))
        duration = int(first.get("duration", 0))

        return {
            "distance": distance,
            "duration": duration,
            "route_type": route_type,
            "description": f"全程约{distance / 1000:.1f}公里,预计{duration // 60}分钟",
        }

    def geocode(self, address: str, city: Optional[str] = None) -> List[dict]:
        """地理编码 (地址/城市名转经纬度与adcode)

        Args:
            address: 地址或城市名
            city: 城市 (可选, 提高解析精度)

        Returns:
            高德 geocodes 列表, 每项含 location/adcode 等字段
        """
        params = {"address": address}
        if city:
            params["city"] = city
        data = self._get("/v3/geocode/geo", params)
        return data.get("geocodes", [])

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """获取POI详情 (含图片等扩展信息)

        Args:
            poi_id: POI ID

        Returns:
            POI详情原始数据(dict)
        """
        data = self._get("/v3/place/detail", {
            "id": poi_id,
            "extensions": "all",
        })
        pois = data.get("pois", [])
        return pois[0] if pois else {}

    def get_poi_photo_by_name(self, name: str) -> Optional[str]:
        """根据景点名称获取图片URL (国内图源: 高德POI图片)

        Unsplash等国外图源在国内网络不稳定, 因此优先使用高德POI自带的实景图片。
        先全国搜索该名称POI, 取第一个结果的 photos; 搜索无图则再查详情。

        Args:
            name: 景点名称

        Returns:
            图片URL (http自动转https以兼容前端混合内容限制); 无图返回None
        """
        # 命中缓存直接返回 (1小时有效), 避免重复消耗高德配额
        cached = self._photo_cache.get(name)
        if cached and cached[1] > time.time():
            return cached[0]

        # QPS熔断: 刚触发过CUQPS超限时, 短时间内直接返回None, 避免继续加重超限
        if time.time() < self._photo_blocked_until:
            return None

        try:
            # 1. 全国搜索该名称的POI
            data = self._get("/v3/place/text", {
                "keywords": name,
                "offset": 1,
                "page": 1,
                "extensions": "all",
            })
            pois = data.get("pois", [])
            if not pois:
                return None

            # 2. 搜索结果自带 photos 字段
            photos = pois[0].get("photos") or []

            # 3. 搜索无图则查详情
            if not photos:
                poi_id = pois[0].get("id", "")
                if poi_id:
                    detail = self.get_poi_detail(poi_id)
                    photos = detail.get("photos") or []

            url = photos[0].get("url", "") if photos else ""
            if not url:
                return None
            # 高德图片URL为http时转https, 避免前端https页面被混合内容拦截
            url = url if url.startswith("https://") else url.replace("http://", "https://", 1)

            # 写入缓存 (1小时有效), 注意缓存None不写入以支持失败后重试
            self._photo_cache[name] = (url, time.time() + 3600)
            return url
        except Exception as e:
            # CUQPS超限(QPS超限)时熔断30秒, 让并发的其他请求短路, 避免集体触发超限
            if "CUQPS_HAS_EXCEEDED_THE_LIMIT" in str(e):
                self._photo_blocked_until = time.time() + 30
            logger.warning(f"高德获取图片失败: {e}")
            return None


# 创建全局服务实例
_amap_service = None


def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)"""
    global _amap_service

    if _amap_service is None:
        _amap_service = AmapService()

    return _amap_service
