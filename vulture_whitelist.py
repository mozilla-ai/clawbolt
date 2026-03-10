# Vulture whitelist: false positives that should not be flagged as dead code.
# See https://github.com/jendrikseipp/vulture#whitelisting

# --- Abstract method parameters (must exist in signature) ---
credentials  # AuthBackend.authenticate_login parameter

# --- Pytest fixtures / mock patches (activated by name, not direct reference) ---
mock_log_usage  # Mock patch injected into test functions
mock_default_channel  # Mock patch injected into test functions
webchat_user  # Fixture that sets up test state as side effect
