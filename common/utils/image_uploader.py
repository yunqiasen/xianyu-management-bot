"""
图片上传器

负责将图片上传到闲鱼CDN
"""
from __future__ import annotations

import json
import os
import tempfile
from io import BytesIO
from typing import Optional

import aiohttp
from loguru import logger
from PIL import Image


class ImageUploader:
    """图片上传器 - 上传图片到闲鱼CDN"""
    
    def __init__(self, cookies_str: str, *, account_id: str | None = None, owner_id: int | None = None):
        self.account_id, self.owner_id = account_id, owner_id
        self.cookies_str = ""
        self.upload_url = "https://stream-upload.goofish.com/api/upload.api?floderId=0&appkey=xy_chat&_input_charset=utf-8"
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def create_session(self):
        """The worker owns the HTTP session; this adapter only prepares image bytes."""
        return None

    async def close_session(self):
        """关闭HTTP会话"""
        if self.session:
            await self.session.close()
            self.session = None
    
    def _compress_image(
        self,
        image_path: str,
        max_size: int = 5 * 1024 * 1024,
        quality: int = 85,
    ) -> Optional[str]:
        """压缩图片"""
        try:
            with Image.open(image_path) as img:
                # 转换为RGB模式
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # 调整尺寸
                original_width, original_height = img.size
                max_dimension = 1920
                if original_width > max_dimension or original_height > max_dimension:
                    if original_width > original_height:
                        new_width = max_dimension
                        new_height = int((original_height * max_dimension) / original_width)
                    else:
                        new_height = max_dimension
                        new_width = int((original_width * max_dimension) / original_height)
                    
                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                    logger.info(f"图片尺寸调整: {original_width}x{original_height} -> {new_width}x{new_height}")
                
                # 创建临时文件
                temp_fd, temp_path = tempfile.mkstemp(suffix='.jpg')
                os.close(temp_fd)
                
                # 保存压缩后的图片
                img.save(temp_path, 'JPEG', quality=quality, optimize=True)
                
                # 检查文件大小
                file_size = os.path.getsize(temp_path)
                if file_size > max_size:
                    quality = max(30, quality - 20)
                    img.save(temp_path, 'JPEG', quality=quality, optimize=True)
                    file_size = os.path.getsize(temp_path)
                    logger.info(f"图片质量调整为 {quality}%，文件大小: {file_size / 1024:.1f}KB")
                
                logger.info(f"图片压缩完成: {file_size / 1024:.1f}KB")
                return temp_path
                
        except Exception as e:
            logger.error(f"图片压缩失败: {e}")
            return None
    
    async def upload_image(self, image_path: str) -> Optional[str]:
        from common.services.image_gateway import upload_account_image
        if not self.account_id or self.owner_id is None:
            logger.warning('图片上传缺少账号执行身份')
            return None
        from common.services.media_paths import owned_media_path
        try:
            image_path = str(owned_media_path(image_path, self.owner_id))
        except ValueError:
            logger.warning('图片资源归属校验失败')
            return None
        temp_path = self._compress_image(image_path)
        if not temp_path:
            return None
        try:
            return await upload_account_image(self.account_id, self.owner_id, temp_path)
        except Exception as exc:
            logger.warning('图片上传结果待核实: {}', type(exc).__name__)
            return None
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def _parse_upload_response(self, response_text: str) -> Optional[str]:
        """解析上传响应获取图片URL"""
        try:
            # 检查是否返回了登录页面
            if '<!DOCTYPE html>' in response_text or '<html>' in response_text:
                logger.error("图片上传失败：Cookie已失效")
                return None
            
            response_data = json.loads(response_text)
            
            # 多种响应格式支持
            if 'data' in response_data and 'url' in response_data['data']:
                return response_data['data']['url']
            
            if 'object' in response_data and isinstance(response_data['object'], dict):
                obj = response_data['object']
                if 'url' in obj:
                    return obj['url']

            if 'url' in response_data:
                return response_data['url']

            if 'result' in response_data and 'url' in response_data['result']:
                return response_data['result']['url']

            if 'data' in response_data and isinstance(response_data['data'], dict):
                data = response_data['data']
                if 'fileUrl' in data:
                    return data['fileUrl']
                if 'file_url' in data:
                    return data['file_url']
            
            logger.error(f"无法从响应中提取图片URL: {response_data}")
            return None
            
        except json.JSONDecodeError:
            logger.error(f"响应不是有效的JSON格式: {response_text[:200]}...")
            return None
        except Exception as e:
            logger.error(f"解析上传响应异常: {e}")
            return None
    
    async def __aenter__(self):
        await self.create_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_session()
