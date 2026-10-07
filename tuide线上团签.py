import requests
from bs4 import BeautifulSoup
import json
import time
import os
import subprocess
import csv
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================== 配置 ==================
CSV_FILE = "TUIDE线上团签.csv"                # CSV 文件名
GITHUB_REPO = "Juineii/flareu_k40513"        # 请替换为您的仓库名
GITHUB_BRANCH = "main"                        # 分支名（main 或 master）
PUSH_INTERVAL = 60                            # 推送检查间隔（秒）

# GitHub Personal Access Token 优先从环境变量 GITHUB_TOKEN 读取

# 商品URL和地址名称映射
product_urls = {
    "https://jp.ktown4u.com/iteminfo?eve_no=44543309&goods_no=170181&grp_no=44543310": "日本地址",
    "https://www.ktown4u.com/iteminfo?eve_no=44543309&goods_no=170181&grp_no=44543310": "国际地址",
    "https://cn.ktown4u.com/iteminfo?eve_no=44543309&goods_no=170181&grp_no=44543310": "中国地址",
    "https://kr.ktown4u.com/iteminfo?eve_no=44543309&goods_no=170181&grp_no=44543310": "韩国地址"
}

# 存储库存数据
last_quantities = {}
initial_stock_printed = {}  # 记录初始库存是否已打印

# 全局锁和计数器
lines_since_last_push = 0   # 自上次推送后又写入了多少行
lines_lock = threading.Lock()
file_lock = threading.Lock()

# ================== Git 推送函数（返回是否成功） ==================
def git_push_update():
    """
    将最新的 CSV 文件提交并推送到 GitHub
    返回: True 表示推送成功, False 表示失败
    """
    try:
        # 获取 GitHub Token（优先从环境变量读取）
        token = os.environ.get('GITHUB_TOKEN')
        if not token:
            print("⚠️ 环境变量 GITHUB_TOKEN 未设置，跳过 Git 推送")
            return False

        # 构建带认证的远程仓库 URL
        remote_url = f"https://{token}@github.com/{GITHUB_REPO}.git"

        # 添加 CSV 文件到暂存区
        subprocess.run(['git', 'add', CSV_FILE], check=True, capture_output=True, timeout=30)

        # 检查是否有文件变化（避免空提交）
        result = subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True, timeout=30)
        if result.returncode != 0:
            # 有变化，提交
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
            commit_msg = f"自动更新数据 {timestamp}"
            subprocess.run(['git', 'commit', '-m', commit_msg], check=True, capture_output=True, timeout=30)

            # 推送到 GitHub（指定分支）
            subprocess.run(
                ['git', 'push', remote_url, f'HEAD:{GITHUB_BRANCH}'],
                check=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            print(f"✅ 已推送到 GitHub: {commit_msg}")
            return True
        else:
            print("⏭️ CSV 文件无变化，跳过推送")
            return True  # 无变化但逻辑上也算成功，避免重复尝试

    except subprocess.TimeoutExpired:
        print("❌ Git 操作超时 (30秒)，推送失败")
        return False
    except subprocess.CalledProcessError as e:
        print(f"❌ Git 操作失败: {e.stderr if e.stderr else e}")
        return False
    except Exception as e:
        print(f"❌ 推送过程中发生错误: {e}")
        return False


def create_session():
    """
    创建带有重试机制和请求头的请求会话
    """
    session = requests.Session()
    # 设置请求头，模拟浏览器
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",  # Do Not Track
        "Upgrade-Insecure-Requests": "1"
    }
    session.headers.update(headers)

    # 设置重试机制
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def fetch_stock_data(url, session):
    """
    从指定URL获取库存数据
    """
    try:
        # 发送HTTP请求
        response = session.get(url, timeout=5)
        response.raise_for_status()  # 抛出HTTP错误

        # 解析HTML
        soup = BeautifulSoup(response.text, "html.parser")
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})

        if not script_tag:
            return None

        # 解析JSON数据
        try:
            json_data = json.loads(script_tag.string)
        except json.JSONDecodeError:
            return None

        # 提取productDetails
        try:
            page_props = json_data.get("props", {}).get("pageProps", {})
            product_details = page_props.get("productDetails")

            if not product_details:
                return None

            quantity = product_details.get("quantity")

            if quantity is None:
                return None

            return quantity

        except (KeyError, TypeError):
            return None

    except requests.exceptions.RequestException:
        return None


def save_to_csv(data):
    """
    将数据保存到CSV文件（追加模式），并累加计数器
    不在此处触发推送，只负责写入文件
    """
    global lines_since_last_push

    try:
        # CSV 列名（顺序）
        fieldnames = ["时间", "商品名称", "库存变化", "单笔销量"]

        # 检查文件是否存在（用于判断是否需要写入表头）
        file_exists = os.path.exists(CSV_FILE)

        # 以追加模式打开文件，newline='' 避免出现空行
        with file_lock:
            with open(CSV_FILE, 'a', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not file_exists:
                    writer.writeheader()  # 文件不存在时写入表头
                writer.writerow(data)     # 写入一行数据

        # 写入成功，计数器加1（线程安全）
        with lines_lock:
            lines_since_last_push += 1
        return True

    except Exception as e:
        print(f"错误 - 无法写入CSV文件: {e}")
        return False


# ================== 推送线程 ==================
def push_worker():
    global lines_since_last_push
    while True:
        time.sleep(PUSH_INTERVAL)
        with lines_lock:
            pending = lines_since_last_push
        if pending > 0:
            print(f"⏰ 定时推送：有 {pending} 条新数据待推送")
            with file_lock:          # 推送期间禁止写入，保证文件完整
                success = git_push_update()
            if success:
                with lines_lock:
                    lines_since_last_push = 0
                print("✅ 推送成功，计数器已归零")
            else:
                print("⚠️ 推送失败，下次再试")


def monitor_stock_changes():
    """
    监控库存变化并记录销量
    """
    session = create_session()  # 创建带重试的会话

    while True:
        # 时间列已存储为文本格式并显示到秒单位
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

        for url, address_name in product_urls.items():
            current_quantity = fetch_stock_data(url, session)

            if current_quantity is None:
                continue

            # 首次获取库存时初始化
            if url not in last_quantities:
                last_quantities[url] = current_quantity

                # 记录初始库存（只记录一次）
                if url not in initial_stock_printed:
                    initial_stock_printed[url] = True

                    # 准备数据
                    stock_change_str = f"初始销量: {current_quantity}"
                    single_sale = abs(current_quantity)

                    data = {
                        "时间": timestamp,
                        "商品名称": address_name,
                        "库存变化": stock_change_str,
                        "单笔销量": single_sale
                    }

                    if save_to_csv(data):
                        print(f"{timestamp} - {address_name}: 初始库存: {current_quantity}")

            else:
                previous_quantity = last_quantities[url]
                sales_change = previous_quantity - current_quantity

                if sales_change != 0:
                    stock_change_str = f"{previous_quantity}->{current_quantity}"
                    single_sale = sales_change

                    data = {
                        "时间": timestamp,
                        "商品名称": address_name,
                        "库存变化": stock_change_str,
                        "单笔销量": single_sale
                    }

                    if save_to_csv(data):
                        print(f"{timestamp} - {address_name}: 库存变化: {previous_quantity}->{current_quantity}, 销量变化: {sales_change}")

                # 更新当前库存
                last_quantities[url] = current_quantity

        # 爬取间隔完全独立，固定 10 秒（可按需修改）
        time.sleep(10)


# ================== 启动 ==================
if __name__ == "__main__":
    # 启动推送守护线程
    push_thread = threading.Thread(target=push_worker, daemon=True)
    push_thread.start()

    try:
        monitor_stock_changes()
    except KeyboardInterrupt:
        print("\n监控程序被用户终止")
        # 退出前推送剩余数据
        with lines_lock:
            pending = lines_since_last_push
        if pending > 0:
            print(f"正在推送剩余的 {pending} 条数据...")
            with file_lock:
                success = git_push_update()
            if success:
                print("✅ 剩余数据已推送")
            else:
                print("⚠️ 剩余数据推送失败，请手动检查")
        else:
            print("无待推送数据")
    except Exception as e:
        print(f"监控程序发生未预期的错误: {e}")