from support import *
from datetime import datetime
from decimal import Decimal
class StatsTests(DatabaseCase):
    async def test_shared_detail_trend_summary_and_tenant(self):
        from app.services.dashboard_stats_service import DashboardStatsService
        service=DashboardStatsService(self.session)
        self.assertTrue(callable(getattr(service,'operating_summary',None)), '缺统一财务口径入口')
        self.session.add_all([XYOrder(id=1,owner_id=1,order_no='a',status='paid',amount=Decimal('10.25'),placed_at=datetime(2026,9,21,8)),XYOrder(id=2,owner_id=1,order_no='b',status='refunded',amount=Decimal('3.15'),placed_at=datetime(2026,9,21,9)),XYOrder(id=3,owner_id=1,order_no='c',status='pending',amount=99,placed_at=datetime(2026,9,21,10)),XYOrder(id=4,owner_id=2,order_no='d',status='paid',amount=999,placed_at=datetime(2026,9,21,10))]); await self.session.commit()
        result=await service.operating_summary(1,'2026-09-21','2026-09-22','Asia/Shanghai')
        self.assertEqual(result['summary'],{'count':2,'paid':'13.40','refund':'3.15','net':'10.25'})
        self.assertEqual(result['trend'][0]['net'],'10.25')
        self.assertEqual(len(result['details']),2)
        self.assertEqual(result['business_available_accounts'],None)
    async def test_existing_trend_excludes_unpaid_and_matches_net_refund(self):
        from app.services.dashboard_stats_service import DashboardStatsService
        from unittest.mock import patch
        self.session.add_all([XYOrder(id=1,owner_id=1,order_no='a',status='paid',amount=10,placed_at=datetime(2026,9,21,10)),XYOrder(id=2,owner_id=1,order_no='b',status='pending',amount=99,placed_at=datetime(2026,9,21,10)),XYOrder(id=3,owner_id=1,order_no='c',status='refunded',amount=4,placed_at=datetime(2026,9,21,10))]);await self.session.commit()
        with patch.object(DashboardStatsService,'_build_today_start',return_value=datetime(2026,9,21)):
            trend=await DashboardStatsService(self.session).get_order_amount_trend(owner_id=1,days=1)
        self.assertEqual(trend[0]['amount'],10.0)
        self.assertEqual(trend[0]['count'],2)
