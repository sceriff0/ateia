def pytest_configure(config):
    config.addinivalue_line("markers", "slow: end-to-end tests that invoke the STARE stages")
