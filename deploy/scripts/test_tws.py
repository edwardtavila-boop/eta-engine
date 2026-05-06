try:
    from ib_insync import IB
    ib = IB()
    ib.connect('127.0.0.1', 4002, clientId=99, timeout=5)
    accounts = ib.managedAccounts()
    print(f"TWS API CONNECTED. Accounts: {accounts}")
    ib.disconnect()
except ModuleNotFoundError:
    print("ib_insync NOT installed — checking ibapi")
    try:
        print("ibapi available")
    except:
        print("No IBKR Python API libraries found")
except Exception as e:
    print(f"Connection error: {e}")
