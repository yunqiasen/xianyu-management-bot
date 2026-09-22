"""
商品发布业务逻辑服务

功能：
1. 素材库 CRUD（创建/查询/更新/删除商品模板）
2. 提供素材字典转换工具，供发布执行链路复用
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.product_material import ProductMaterial
from app.services.xianyu_item_snapshot import as_bool


# ==================== 素材库服务 ====================

from common.utils.time_utils import safe_isoformat


class MaterialSpecificationError(ValueError):
    """商品素材规格不符合保存规则。"""


def _normalize_specifications(value: Any) -> list[dict]:
    """Validate without truncating dimensions or dropping source identities."""
    from copy import deepcopy
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 2:
        raise MaterialSpecificationError("规格应为列表且至多两组")
    normalized, names = [], set()
    for spec in value:
        if not isinstance(spec, dict):
            raise MaterialSpecificationError("规格结构错误")
        name = str(spec.get("name") or "").strip()
        if not name or name in names:
            raise MaterialSpecificationError("规格名称缺失或重复")
        names.add(name)
        values, seen = [], set()
        raw_values = spec.get("values")
        if not isinstance(raw_values, list) or not 1 <= len(raw_values) <= 50:
            raise MaterialSpecificationError(f"规格“{name}”需要1至50个值")
        for item in raw_values:
            if not isinstance(item, dict):
                raise MaterialSpecificationError("规格值结构错误")
            label = str(item.get("name") or "").strip()
            if not label or label in seen:
                raise MaterialSpecificationError(f"规格“{name}”存在缺失或重复规格值")
            seen.add(label)
            values.append({**deepcopy(item), "name": label, "image": item.get("image") or None})
        normalized.append({**deepcopy(spec), "name": name,
                           "support_image": as_bool(spec.get("support_image", False)), "values": values})
    return normalized


def _normalize_sku_rows(value: Any) -> list[dict]:
    from copy import deepcopy
    from decimal import Decimal, InvalidOperation
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 200:
        raise MaterialSpecificationError("SKU应为列表且至多200条")
    normalized = []
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("specs"), dict):
            raise MaterialSpecificationError("SKU规格结构错误")
        try:
            price, stock = Decimal(str(row.get("price"))), Decimal(str(row.get("stock")))
            if not price.is_finite() or price <= 0 or not stock.is_finite() or stock < 0 or stock != stock.to_integral_value():
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise MaterialSpecificationError("SKU价格需为正数，库存需为非负整数") from None
        normalized.append({**deepcopy(row), "specs": {str(k): str(v) for k, v in row["specs"].items()},
                           "price": float(price), "stock": int(stock)})
    return normalized


def _normalize_material_json(data: dict) -> dict:
    from copy import deepcopy
    from itertools import product
    normalized = deepcopy(data)
    specs = _normalize_specifications(data.get("specifications"))
    rows = _normalize_sku_rows(data.get("sku_rows"))
    names = [spec["name"] for spec in specs]
    expected = set(product(*[[v["name"] for v in s["values"]] for s in specs])) if specs else set()
    seen, source_ids = set(), set()
    for row in rows:
        key = tuple(row["specs"].get(name) for name in names)
        if set(row["specs"]) != set(names) or key not in expected or key in seen:
            raise MaterialSpecificationError("SKU组合缺失、重复或引用未知规格")
        seen.add(key)
        source_id = row.get("source_id")
        if source_id and str(source_id) in source_ids:
            raise MaterialSpecificationError("SKU来源ID重复")
        if source_id:
            source_ids.add(str(source_id))
    if seen != expected:
        raise MaterialSpecificationError("SKU未覆盖全部规格组合")
    normalized.update(specifications=specs, sku_rows=rows)
    for key in ("platform_category_path", "platform_attributes", "videos", "images"):
        normalized[key] = deepcopy(data.get(key) or [])
    return normalized


class ProductMaterialService:
    """商品素材库 CRUD 服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: int, data: dict) -> ProductMaterial:
        """创建素材"""
        data = _normalize_material_json(data)
        shipping_method = str(data.get("shipping_method") or "free")
        material = ProductMaterial(
            user_id=user_id,
            title=data["title"],
            description=data["description"],
            price=float(data["price"]),
            original_price=float(data["original_price"]) if data.get("original_price") else None,
            category=data.get("category"),
            platform_category_id=data.get("platform_category_id"),
            platform_category_name=data.get("platform_category_name"),
            platform_channel_category_id=data.get("platform_channel_category_id"),
            platform_channel_category_name=data.get("platform_channel_category_name"),
            platform_leaf_id=data.get("platform_leaf_id"),
            platform_tb_category_id=data.get("platform_tb_category_id"),
            platform_category_path=data.get("platform_category_path") or [],
            platform_attributes=data.get("platform_attributes") or [],
            category_source=data.get("category_source") or "manual",
            category_confidence=data.get("category_confidence"),
            images=data["images"],
            videos=data.get("videos") or [],
            specifications=data["specifications"],
            sku_rows=data["sku_rows"],
            quantity=int(data.get("quantity") or 1),
            # delivery_method 仅为兼容展示字段，实际发布由 shipping_method 决定。
            delivery_method="pickup" if shipping_method == "none" else "express",
            shipping_method=shipping_method,
            # 兼容 API/历史调用可能传入的 "true"/"false" 字符串，避免 bool("false") 为 True。
            support_pickup=as_bool(data.get("support_pickup", False)),
            postage=float(data.get("postage", 0)),
            address=data.get("address"),
            address_expected_text=data.get("address_expected_text"),
            brand=data.get("brand"),
            condition=data.get("condition", "全新"),
            remark=data.get("remark"),
        )
        self.session.add(material)
        await self.session.commit()
        await self.session.refresh(material)
        return material

    async def list_materials(
        self, user_id: int = None, page: int = 1, page_size: int = 20,
        title: str = None, category: str = None, condition: str = None,
        platform_category_id: str = None,
    ) -> Dict[str, Any]:
        """分页查询素材列表
        
        Args:
            user_id: 用户ID，为None时查询全部（管理员场景）
            title: 标题模糊搜索
            category: 分类筛选
            condition: 成色筛选
        """
        page = max(page, 1)
        page_size = page_size if page_size in (10, 20, 50, 100, 500, 1000) else 20

        base_cond = [ProductMaterial.is_deleted.is_(False)]
        if user_id is not None:
            base_cond.append(ProductMaterial.user_id == user_id)
        if title:
            base_cond.append(ProductMaterial.title.ilike(f"%{title}%"))
        if category:
            base_cond.append(ProductMaterial.category == category)
        if condition:
            base_cond.append(ProductMaterial.condition == condition)
        if platform_category_id:
            base_cond.append(ProductMaterial.platform_category_id == platform_category_id)

        count_stmt = (
            select(func.count())
            .select_from(ProductMaterial)
            .where(*base_cond)
        )
        total = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = (
            select(ProductMaterial)
            .where(*base_cond)
            .order_by(desc(ProductMaterial.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.session.execute(stmt)).scalars().all()

        return {
            "list": [_material_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        }

    async def get(self, material_id: int, user_id: int = None) -> Optional[ProductMaterial]:
        """查询单条素材
        
        Args:
            material_id: 素材ID
            user_id: 用户ID，为None时不限用户（管理员场景）
        """
        conds = [ProductMaterial.id == material_id, ProductMaterial.is_deleted.is_(False)]
        if user_id is not None:
            conds.append(ProductMaterial.user_id == user_id)
        stmt = select(ProductMaterial).where(*conds)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_ids(self, material_ids: List[int], user_id: int) -> List[ProductMaterial]:
        if not material_ids:
            return []
        unique_ids = list(dict.fromkeys(material_ids))
        stmt = select(ProductMaterial).where(
            ProductMaterial.user_id == user_id,
            ProductMaterial.id.in_(unique_ids),
            ProductMaterial.is_deleted.is_(False),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        material_map = {row.id: row for row in rows}
        return [material_map[mid] for mid in material_ids if mid in material_map]

    async def update(self, material_id: int, user_id: int = None, data: dict = None) -> Optional[ProductMaterial]:
        """更新素材（user_id=None时管理员可操作任意素材）"""
        data = data or {}
        material = await self.get(material_id, user_id)
        if not material:
            return None

        if "specifications" in data or "sku_rows" in data:
            merged = _normalize_material_json({
                "specifications": material.specifications, "sku_rows": material.sku_rows, **data,
            })
            data = {**data, "specifications": merged["specifications"], "sku_rows": merged["sku_rows"]}

        updatable = [
            "title", "description", "price", "original_price", "category",
            "platform_category_id", "platform_category_name",
            "platform_channel_category_id", "platform_channel_category_name",
            "platform_leaf_id", "platform_tb_category_id", "platform_category_path", "platform_attributes",
            "category_source", "category_confidence", "images", "videos", "specifications", "sku_rows", "quantity",
            "delivery_method", "shipping_method", "support_pickup", "postage", "address", "address_expected_text", "brand", "condition", "remark",
        ]
        for field in updatable:
            if field in data:
                value = data[field]
                if field in ("price", "original_price", "postage"):
                    value = float(value) if value else (None if field == "original_price" else 0)
                elif field == "support_pickup":
                    # 兼容表单/历史调用传入的布尔字符串，避免 bool("false") 被当作 True。
                    value = as_bool(value)
                setattr(material, field, value)

        # 防止历史调用只更新 delivery_method 造成与实际运费方式不一致。
        material.delivery_method = "pickup" if material.shipping_method == "none" else "express"

        await self.session.commit()
        await self.session.refresh(material)
        return material

    async def delete(self, material_id: int, user_id: int = None) -> bool:
        """删除素材（user_id=None时管理员可操作任意素材）"""
        material = await self.get(material_id, user_id)
        if not material:
            return False
        material.is_deleted = True
        await self.session.commit()
        return True

    async def batch_delete(self, material_ids: List[int], user_id: int = None) -> int:
        """批量删除素材，返回实际删除数量
        
        Args:
            material_ids: 素材ID列表
            user_id: 用户ID，为None时管理员可操作任意素材
        """
        if not material_ids:
            return 0
        conds = [ProductMaterial.id.in_(material_ids), ProductMaterial.is_deleted.is_(False)]
        if user_id is not None:
            conds.append(ProductMaterial.user_id == user_id)
        stmt = select(ProductMaterial).where(*conds)
        rows = (await self.session.execute(stmt)).scalars().all()
        for row in rows:
            row.is_deleted = True
        await self.session.commit()
        return len(rows)


# ==================== 工具函数 ====================

def _material_to_dict(m: ProductMaterial) -> dict:
    """将素材模型转为字典"""
    shipping_method = m.shipping_method or ("fixed" if m.postage else "free")
    return {
        "id": m.id,
        "user_id": m.user_id,
        "title": m.title,
        "description": m.description,
        "price": float(m.price) if m.price is not None else 0,
        "original_price": float(m.original_price) if m.original_price is not None else None,
        "category": m.category,
        "platform_category_id": m.platform_category_id,
        "platform_category_name": m.platform_category_name,
        "platform_channel_category_id": m.platform_channel_category_id,
        "platform_channel_category_name": m.platform_channel_category_name,
        "platform_leaf_id": m.platform_leaf_id,
        "platform_tb_category_id": m.platform_tb_category_id,
        "platform_category_path": m.platform_category_path or [],
        "platform_attributes": m.platform_attributes or [],
        "category_source": m.category_source or "manual",
        "category_confidence": float(m.category_confidence) if m.category_confidence is not None else None,
        "images": m.images or [],
        "videos": m.videos or [],
        "specifications": m.specifications or [],
        "sku_rows": m.sku_rows or [],
        "quantity": m.quantity or 1,
        # shipping_method 是实际发布依据；兼容历史记录中的旧/空 delivery_method。
        "delivery_method": "pickup" if shipping_method == "none" else "express",
        "shipping_method": shipping_method,
        "support_pickup": as_bool(m.support_pickup),
        "postage": float(m.postage) if m.postage is not None else 0,
        "address": m.address,
        "address_expected_text": m.address_expected_text,
        "brand": m.brand,
        "condition": m.condition,
        "remark": m.remark,
        "created_at": safe_isoformat(m.created_at),
        "updated_at": safe_isoformat(m.updated_at),
    }
