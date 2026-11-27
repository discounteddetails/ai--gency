from fastapi.testclient import TestClient

from scripts.import_onepagecrm import rows_from_csv


def test_onepagecrm_import_parsing(tmp_path):
    p = tmp_path / "contacts.csv"
    p.write_text("First name,Last name,Phones,Emails,Tags,Notes\n"
                 "Maria,Gonzalez,(512) 555-0182,Maria@Example.com,customer,Full detail Oct\n"
                 ",,,,,\n"
                 "Jo,,512.555.0199,,lead,\n", encoding="utf-8")
    rows = rows_from_csv(str(p))
    assert len(rows) == 2
    assert rows[0]["phone"] == "+15125550182" and rows[0]["email"] == "maria@example.com"
    assert rows[0]["lead_status"] == "Customer" and rows[1]["lead_status"] == "New"


def test_health_app():
    from app.main import health_only_app

    assert TestClient(health_only_app()).get("/healthz").json() == {"ok": True}


def test_system_prompt_loads_without_header():
    from app.agent.prompt import system_prompt

    p = system_prompt()
    assert p.startswith("You are the booking and customer assistant for **Discounted Details**")
    assert "This file is the agent's system prompt" not in p
