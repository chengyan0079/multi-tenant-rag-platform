"""Nacos 配置中心连通性检查（只读，不写任何配置）。

用法：
  python tests/check_nacos_connectivity.py
  NACOS_CONFIG_SERVER_ADDR=http://x.x.x.x:8848 NACOS_CONFIG_USER=nacos NACOS_CONFIG_PASSWORD=nacos \
      python tests/check_nacos_connectivity.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import config
from core.config_center.nacos_config import ConfigCenterClient, ConfigCenterError


def _env(name: str, default: str) -> str:
    """环境变量优先，未设则回落 common.config（避免脚本里再抄一份默认值）。"""
    return os.environ.get(name, default)


def main() -> None:
    addr = _env('NACOS_CONFIG_SERVER_ADDR', config.NACOS_CONFIG_SERVER_ADDR)
    ns = _env('NACOS_CONFIG_NAMESPACE', config.NACOS_CONFIG_NAMESPACE)
    group = _env('NACOS_CONFIG_GROUP', config.NACOS_CONFIG_GROUP)
    user = _env('NACOS_CONFIG_USER', config.NACOS_CONFIG_USER) or None
    pwd = _env('NACOS_CONFIG_PASSWORD', config.NACOS_CONFIG_PASSWORD) or None

    print('Nacos 配置中心连通性检查')
    print(f'  server_addr = {addr}')
    print(f'  namespace   = {ns}')
    print(f'  group       = {group}')
    print(f'  auth        = {"on" if user else "off"}')

    client = ConfigCenterClient(
        server_addr=addr, namespace=ns, group=group,
        user=user, password=pwd, timeout=10.0,
    )

    # 1) 配置列表（cs 端点，配置中心真实读路径）：验证能连通 + 能读目标命名空间
    try:
        cfgs = client.list_configs(page_size=10)
    except ConfigCenterError as e:
        print(f'[FAIL] list_configs 失败: {e}')
        sys.exit(1)
    except Exception as e:  # 网络层异常（连接拒绝等）
        print(f'[FAIL] list_configs 网络异常: {type(e).__name__}: {e}')
        sys.exit(1)
    total = (cfgs or {}).get('totalCount')
    items = (cfgs or {}).get('pageItems') or []
    print(f'[OK ] list_configs 返回 total={total} 本页 {len(items)} 条')
    for it in items[:10]:
        print(f'      - {it.get("dataId")}  group={it.get("group")}  md5={it.get("md5")}')

    # 2) 单配置读取（探测一个不存在的 data_id，端点通即可，预期返回 None）
    probe = 'connectivity-probe.json'
    try:
        got = client.get_config(probe)
        print(f'[OK ] get_config("{probe}") 端点可用，返回: {got!r}')
    except ConfigCenterError as e:
        print(f'[WARN] get_config 失败: {e}')

    # 3) 命名空间列表（console 端点，仅辅助；部分部署不暴露该端点，失败不致命）
    try:
        ns_list = client.list_namespaces()
        print(f'[OK ] list_namespaces 返回 {len(ns_list)} 个命名空间')
        target = [n for n in ns_list
                  if (n.get('namespace') == ns or n.get('namespaceShowName') == ns)]
        if target:
            print(f'[OK ] 目标命名空间 "{ns}" 存在: {target[0]}')
        else:
            print(f'[WARN] 未在返回列表找到 "{ns}"（可能无权限或命名空间名不一致）')
    except Exception as e:
        print(f'[WARN] list_namespaces 不可用（{type(e).__name__}: {e}）；'
              f'不影响配置中心 cs 端点，可忽略')

    print('连通性检查完成。')


if __name__ == '__main__':
    main()
