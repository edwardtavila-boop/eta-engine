
from ib_insync import IB

ib = IB()
ib.connect('127.0.0.1', 4002, clientId=101, timeout=5)
trades = ib.trades()
print(f"IBKR Trades: {len(trades)}")
for t in trades[-10:]:
    print(f"  {t.contract.symbol} {t.order.action} x{t.order.totalQuantity} "
          f"{t.order.orderType} status={t.orderStatus.status} "
          f"filled={t.orderStatus.filled} permId={t.order.permId}")
ib.disconnect()
