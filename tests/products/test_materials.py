import copy
import unittest
from stdlib_loader import load

m = load('backend-web/app/services/product_publish_service.py')

class MaterialContractTests(unittest.TestCase):
    def sample(self):
        return {'specifications': [{'name': '颜色', 'source_id': 'pid-7', 'support_image': False,
            'values': [{'name': '蓝', 'image': '/static/blue.png', 'source_id': 'vid-8'}]}],
            'sku_rows': [{'specs': {'颜色': '蓝'}, 'price': 12, 'stock': 2, 'source_id': 'sku-9'}]}

    def test_source_ids_survive_old_json_roundtrip(self):
        value = self.sample()
        self.assertEqual(m._normalize_material_json(value), m._normalize_material_json(m._normalize_material_json(value)))
        actual = m._normalize_material_json(value)
        self.assertEqual(actual['specifications'][0].get('source_id'), 'pid-7')
        self.assertEqual(actual['specifications'][0]['values'][0].get('source_id'), 'vid-8')
        self.assertEqual(actual['sku_rows'][0].get('source_id'), 'sku-9')

    def test_unknown_sku_value_rejected(self):
        value = self.sample(); value['sku_rows'][0]['specs']['颜色'] = '红'
        with self.assertRaises(m.MaterialSpecificationError): m._normalize_material_json(value)

    def test_missing_combination_rejected(self):
        value = self.sample(); value['specifications'][0]['values'].append({'name': '红'})
        with self.assertRaises(m.MaterialSpecificationError): m._normalize_material_json(value)

    def test_duplicate_dimension_rejected(self):
        value = self.sample(); value['specifications'] *= 2
        with self.assertRaises(m.MaterialSpecificationError): m._normalize_material_json(value)

    def test_invalid_stock_and_nan_rejected_not_silently_dropped(self):
        for price, stock in [(float('nan'), 1), (float('inf'), 1), (10, 1.5), (10, -1)]:
            with self.subTest(price=price, stock=stock):
                value = self.sample(); value['sku_rows'][0].update(price=price, stock=stock)
                with self.assertRaises(m.MaterialSpecificationError): m._normalize_material_json(value)

    def test_snapshot_is_detached(self):
        value = self.sample(); actual = m._normalize_material_json(value)
        value['specifications'][0]['values'][0]['name'] = 'changed'
        self.assertEqual(actual['specifications'][0]['values'][0]['name'], '蓝')
