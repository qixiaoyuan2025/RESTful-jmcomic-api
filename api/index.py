from io import BytesIO
from flask import Flask, Response, jsonify, request
from jmcomic import *
import requests
import logging
import struct

import sys

sys.stdout.reconfigure(encoding="utf-8")

app = Flask(__name__)
BASE_API_URL = "https://www,qixiaoyuan.site"
app.debug = False
app.json.ensure_ascii = False

# 存储捕获的图片
captured_images = {}

# 保存原始的save_image方法
original_save_image = JmImageTool.save_image


@classmethod
def new_save_image(cls, image: Image.Image, filepath: str):
    """
    新的save_image方法，捕获PIL.Image对象
    """
    # 捕获图片对象
    captured_images[filepath] = image

    # 调用原始方法保存文件
    # return original_save_image(image, filepath)


JmImageTool.save_image = new_save_image

# original_try_mkdir = JmcomicText.try_mkdir


@classmethod
def new_try_mkdir(cls, save_dir: str):
    return save_dir


JmcomicText.try_mkdir = new_try_mkdir

from urllib.parse import unquote, quote
import re


def decode_search_value(value: str) -> str:
    """
    判断并解码搜索值
    如果值是URL编码，则解码为中文，否则直接返回
    """
    # URL编码的特征：包含%后跟两个十六进制字符
    url_encoded_pattern = r"%[0-9A-Fa-f]{2}"

    # 如果包含URL编码特征，尝试解码
    if re.search(url_encoded_pattern, value):
        try:
            decoded = unquote(value)
            # 解码后如果还包含URL编码特征，说明可能有多重编码，继续解码
            while re.search(url_encoded_pattern, decoded):
                temp = unquote(decoded)
                if temp == decoded:  # 如果没有变化，停止解码
                    break
                decoded = temp
            return decoded
        except Exception:
            # 如果解码失败，返回原值
            return value
    else:
        # 没有URL编码特征，直接返回
        return value


def is_truthy_arg(name: str) -> bool:
    value = request.args.get(name, "0")
    return value in ("1", "true", "True", "yes", "on")


def normalize_rgb_image(image):
    if image.mode in ["RGBA", "LA", "P"]:
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode in ["RGBA", "LA"]:
            background.paste(image, mask=image.split()[-1])
        else:
            background.paste(image)
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def optimize_png_image(image, quality: int):
    quality = max(1, min(100, quality))
    image = normalize_rgb_image(image)

    if quality >= 95:
        return image

    colors = max(16, min(256, int(16 + quality * 2.4)))
    return image.quantize(
        colors=colors,
        method=Image.Quantize.MEDIANCUT,
    )


def build_image_response(image, filename_base: str, quality: int = 50):
    img_io = BytesIO()

    if is_truthy_arg("ifPNG"):
        image = optimize_png_image(image, quality)
        image.save(
            img_io,
            "PNG",
            optimize=True,
            compress_level=9,
        )
        mimetype = "image/png"
        filename = f"{filename_base}.png"
    else:
        image = normalize_rgb_image(image)
        image.save(img_io, "JPEG", quality=quality, optimize=True)
        mimetype = "image/jpeg"
        filename = f"{filename_base}.jpg"

    img_io.seek(0)
    return Response(
        img_io.getvalue(),
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "public, max-age=86400",
            "Content-Type": mimetype,
        },
    )


@app.get("/")
def read_root():
    return """
it works!
    """


@app.get("/album/<int:item_id>/cover")
def get_album_cover(item_id: int):
    """返回封面响应"""
    try:
        a = JmOption.default().new_jm_client()

        a.download_album_cover(item_id, "./cover.webp", "_3x4")

        # 检查是否捕获到图片
        if not captured_images:
            return jsonify({"code": 404, "message": "No image captured"}), 404

        # 获取第一个捕获的图片
        image = next(iter(captured_images.values()))
        captured_images.clear()  # 清空捕获的图片

        # ========== 图片压缩和尺寸限制 ==========
        # 获取原始尺寸
        original_width, original_height = image.size
        print(f"原始图片尺寸: {original_width}x{original_height}")

        # 设置最大宽度，针对IoT设备优化
        width = request.args.get("w")
        max_width = int(width) if width and width.isdigit() else 200

        if original_width > max_width:
            # 计算等比例缩放后的高度
            new_height = int((max_width / original_width) * original_height)
            # 使用高质量的重采样算法
            image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)
            print(f"压缩后尺寸: {max_width}x{new_height}")
        else:
            print(f"图片宽度 {original_width}px 已小于限制 {max_width}px，无需压缩")

        return build_image_response(image, f"{item_id}_cover", 50)

    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500


@app.get("/search/<value>")
@app.get("/search/<value>/")
@app.get("/search/<value>/<int:client_page>")
def get_search(value, client_page=1):
    try:
        client = JmOption.default().new_jm_client()

        search_keyword = decode_search_value(value)
        print(f"原始值: {value}, 解码后: {search_keyword}")

        # 客户端分页设置
        client_page_size = 10
        # API分页设置
        api_page_size = JmModuleConfig.PAGE_SIZE_SEARCH

        # 计算对应的API页码
        # 公式解释: 客户端第N页的起始数据编号为 (N-1)*client_page_size + 1
        # 这个编号在哪个API页呢？用这个编号除以API每页大小，再向上取整。
        api_page = ((client_page - 1) * client_page_size) // api_page_size + 1

        # 计算在该API页内的起始索引 (从0开始)
        start_index_in_api_page = ((client_page - 1) * client_page_size) % api_page_size

        # 请求对应的API页面
        page: JmSearchPage = client.search_site(
            search_query=search_keyword, page=api_page
        )

        # 收集当前API页的所有结果
        all_results_in_api_page = []
        for album_id, title in page:
            all_results_in_api_page.append({"album_id": album_id, "title": title})

        # 从API页结果中截取客户端需要的那10条
        end_index_in_api_page = start_index_in_api_page + client_page_size
        client_results = all_results_in_api_page[
            start_index_in_api_page:end_index_in_api_page
        ]

        # 计算客户端总页数 (基于API报告的总数)
        total_client_pages = (page.total + client_page_size - 1) // client_page_size

        # 判断是否还有更多页
        has_more = client_page < total_client_pages

        # 获取API URL用于构建封面链接
        api_url = BASE_API_URL

        # 构建符合文档要求的搜索结果格式
        results = []
        for item in client_results:
            results.append(
                {
                    "comic_id": item["album_id"],
                    "title": item["title"],
                    "cover_url": f"{api_url}/album/{item['album_id']}/cover",
                    "pages": 0,
                }
            )

        return jsonify({"page": client_page, "has_more": has_more, "results": results})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500


@app.get("/album/<int:item_id>")
@app.get("/album/<int:item_id>/")
def get_album_info(item_id: int, impl="html", url=["18comic.vip"]):
    try:
        a = JmOption.construct(
            {
                "client": {
                    "impl": impl,
                    "domain": url,
                },
                "plugins": {
                    "after_init": [
                        {
                            "plugin": "login",
                            "kwargs": {
                                "username": "test19195456546",
                                "password": "test19195456546",
                            },
                        }
                    ]
                },
            }
        )
        # 客户端
        client = a.new_jm_client(impl=impl)
        # 本子实体类
        album: JmAlbumDetail = client.get_album_detail(item_id)

        photo_detail = client.get_photo_detail(item_id)
        total_pages = len(photo_detail)

        cover_url = f"{BASE_API_URL}/album/{item_id}/cover"

        return jsonify(
            {
                "item_id": item_id,
                "name": album.name,
                "page_count": total_pages,
                "views": album.views,
                "cover": cover_url,
                "tags": album.tags,
                "total_chapters": len(album.episode_list),
            }
        )
    except Exception as e:
        if str(e).find("只对登录用户可见") != -1 and impl != "api":
            print("只对登录用户可见", str(e))
            return get_album_info(item_id, impl="api", url=[])
        if str(e).find("请求重试全部失败") != -1:
            print("请求重试全部失败", str(e))
            return get_album_info(item_id, url=[])
        if str(e).find("403") != -1 and str(e).find("ip地区禁止访问") != -1:
            print("ip地区禁止访问", str(e))
            return get_album_info(item_id, impl="api", url=[])
        return jsonify({"code": 500, "message": str(e)}), 500


@app.get("/photo/<int:item_id>/chapter/<int:chapter>")
def get_photo_chapter(item_id: int, chapter: int = 1):
    try:
        a = JmOption.default().new_jm_client()

        thisChapter: JmAlbumDetail = a.get_album_detail(item_id)
        print(thisChapter.episode_list[chapter - 1][0])

        if len(thisChapter.episode_list) < 1 or chapter > len(thisChapter.episode_list):
            return jsonify({"code": 404, "message": "Chapter not found"}), 404

        photo_id = thisChapter.episode_list[chapter - 1][0]

        photo_detail: JmPhotoDetail = a.get_photo_detail(photo_id)

        api_url = request.host_url.rstrip("/")

        images = [
            {
                "url": f"{BASE_API_URL}/image/proxy?url={api_url}/photo/{photo_id}/{photo_id}_{page_num}.jpg"
            }
            for page_num in range(1, len(photo_detail) + 1)
        ]

        return jsonify({"title": photo_detail.name, "images": images})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500


# 在Flask路由部分添加图片代理接口
@app.get("/image/proxy")
def image_proxy():
    """图片代理接口，处理图片尺寸和质量"""
    try:
        image_url = request.args.get("url")
        if not image_url:
            return jsonify({"error": "缺少url参数"}), 400

        # 设置目标宽度和图片质量，默认值针对IoT设备优化
        target_width = int(request.args.get("width", 600))
        quality = int(request.args.get("quality", 50))

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        resp = requests.get(image_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return jsonify({"error": f"图片下载失败: {resp.status_code}"}), 500

        # 处理图片
        original_image = Image.open(BytesIO(resp.content))

        # 计算新高度，保持宽高比
        width_percent = target_width / float(original_image.size[0])
        target_height = int(float(original_image.size[1]) * float(width_percent))

        # 调整图片尺寸
        resized_image = original_image.resize(
            (target_width, target_height), Image.Resampling.LANCZOS
        )

        if is_truthy_arg("ifLVGL"):
            output_buffer = _convert_to_lvgl8(resized_image)
            return Response(
                output_buffer.getvalue(),
                mimetype="application/octet-stream",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Content-Type": "application/octet-stream",
                },
            )

        # 保存为JPEG或PNG格式
        return build_image_response(resized_image, "image", quality)

    except Exception as e:
        logging.error(f"图片处理失败: {str(e)}")
        return jsonify({"error": f"图片处理失败: {str(e)}"}), 500


def _convert_to_lvgl8(image):
    """将PIL Image转换为LVGL8 indexed-8二进制格式"""
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    image = image.quantize(colors=256, method=Image.Quantize.MEDIANCUT)

    w, h = image.size

    raw_palette = image.getpalette()
    palette = []
    for i in range(256):
        idx = i * 3
        if idx + 2 < len(raw_palette):
            palette.append(
                (raw_palette[idx], raw_palette[idx + 1], raw_palette[idx + 2])
            )
        else:
            palette.append((0, 0, 0))

    cf = 10
    always_zero = 0
    reserved = 0

    header_word1 = cf | (always_zero << 5) | (reserved << 8) | (w << 10) | (h << 21)

    output = BytesIO()
    output.write(struct.pack("<I", header_word1))

    for r, g, b in palette:
        output.write(bytes([b, g, r, 0xFF]))

    output.write(image.tobytes())

    return output


@app.get("/photo/<int:item_id>")
@app.get("/photo/<int:item_id>/")
@app.get("/photo/<int:item_id>/<page>")
def get_image(item_id: int, page: str = "0_1.jpg"):
    """返回图片响应"""
    try:
        # 从字符串中提取页码，格式为 "item_id_page.jpg"
        if "_" in page and page.endswith(".jpg"):
            page_num = int(page.split("_")[1].replace(".jpg", ""))
        else:
            page_num = int(page)

        class ImageDownloader(JmDownloader):
            def do_filter(self, detail):
                if detail.is_photo():
                    photo: JmPhotoDetail = detail
                    # 支持[start,end,step]
                    return photo[page_num - 1 : page_num]
                return detail

        JmModuleConfig.CLASS_DOWNLOADER = ImageDownloader

        # 下载图片
        download_photo(item_id)

        # 检查是否捕获到图片
        if not captured_images:
            return jsonify({"code": 404, "message": "No image captured"}), 404

        print(captured_images)

        # 获取第一个捕获的图片
        image = next(iter(captured_images.values()))
        captured_images.clear()  # 清空捕获的图片

        # ========== 图片压缩和尺寸限制 ==========
        # 获取原始尺寸
        original_width, original_height = image.size
        print(f"原始图片尺寸: {original_width}x{original_height}")

        # 使用传入的文件名
        filename_base = (
            page.rsplit(".", 1)[0] if "." in page else f"{item_id}_{page_num}"
        )

        return build_image_response(image, filename_base)

    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500


@app.get("/config")
@app.get("/config/")
def config():
    api_url = request.host_url.rstrip("/")
    return jsonify(
        {
            "JMComic": {
                "name": "JMComic",
                "apiUrl": api_url,
                "detailPath": "/album/<id>",
                "photoPath": "/photo/<id>/chapter/<chapter>",
                "searchPath": "/search/<text>/<page>",
                "type": "jmcomic",
            },
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
