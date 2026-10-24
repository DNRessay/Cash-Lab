import os

# Must run before `app` is imported: the engine and settings are created at import time.
os.environ["DATABASE_URL"] = "sqlite:///./test_clab.db"
os.environ["SECRET_KEY"] = "test-secret-test-secret-test-secret-xx"
os.environ["SIGNUP_EMAILS"] = "csvuser@inv.example.com,nosy@inv.example.com,eeuser@inv.example.com,soldout@inv.example.com,banker@inv.example.com,blogger@inv.example.com,aiuser@inv.example.com,newbie@inv.example.com,mcpuser@inv.example.com,mcpother@inv.example.com,divcalendar@inv.example.com,jobs@inv.example.com,history@inv.example.com"

if os.path.exists("test_clab.db"):
    os.remove("test_clab.db")
