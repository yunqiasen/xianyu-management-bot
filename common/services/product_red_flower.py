"""One fixed worker command; no local platform session or retry."""
from common.services.account_business_client import dispatch_business

async def request_red_flower(account, order_no):
    return await dispatch_business(account, 'request_red_flower', {'order_no':order_no})
