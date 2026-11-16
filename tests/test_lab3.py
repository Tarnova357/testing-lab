import os
import sys
import time
import uuid
import subprocess
import pytest
import requests
from pathlib import Path
from testcontainers.community.postgres import PostgresContainer
import psycopg


@pytest.fixture(scope="session")
def postgres_env():
    with PostgresContainer("postgres:17-alpine") as postgres:
        env = os.environ.copy()
        env["DB_HOST"] = postgres.get_container_host_ip()
        env["DB_PORT"] = str(postgres.get_exposed_port(5432))
        env["DB_NAME"] = postgres.dbname
        env["DB_USER"] = postgres.username
        env["DB_PASSWORD"] = postgres.password

        conn_str = f"postgresql://{postgres.username}:{postgres.password}@{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}/{postgres.dbname}"
        migration_path = Path(__file__).parent.parent / "migrations" / "V1__create_tables.sql"

        with open(migration_path, "r", encoding="utf-8") as f:
            migration_sql = f.read()

        with psycopg.connect(conn_str, autocommit=True) as conn:
            conn.execute(migration_sql)

        yield env


@pytest.fixture(scope="session")
def live_server(postgres_env):
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "rental.api:app", "--host", "127.0.0.1", "--port", "8080"],
        env=postgres_env
    )

    time.sleep(2)

    base_url = "http://127.0.0.1:8080"
    yield base_url

    process.terminate()
    process.wait()


def create_unique_equipment(base_url: str, daily_rate: int = 10000) -> int:
    code = f"EQ-{uuid.uuid4().hex[:8]}"
    response = requests.post(f"{base_url}/equipment", json={
        "code": code,
        "daily_rate": daily_rate,
        "stock": 10
    })
    response.raise_for_status()
    return response.json()["id"]


def test_create_resource_post(live_server):
    base_url = live_server
    eq_id = create_unique_equipment(base_url, daily_rate=15000)
    ref = f"REF-{uuid.uuid4().hex[:8]}"

    response = requests.post(f"{base_url}/rentals", json={
        "reference": ref,
        "equipment_id": eq_id,
        "quantity": 3,
        "days": 4,
        "premium": False
    })

    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert isinstance(data["id"], int)
    assert data["reference"] == ref
    assert data["status"] == "Active"
    assert data["total"] == 180000
    assert data["late_fee"] == 0


def test_get_resource_by_id(live_server):
    base_url = live_server
    eq_id = create_unique_equipment(base_url)
    ref = f"REF-{uuid.uuid4().hex[:8]}"

    create_resp = requests.post(f"{base_url}/rentals", json={
        "reference": ref, "equipment_id": eq_id, "quantity": 1, "days": 2, "premium": False
    })
    rental_id = create_resp.json()["id"]

    response = requests.get(f"{base_url}/rentals/{rental_id}")

    assert response.status_code == 200
    assert response.json()["id"] == rental_id
    assert response.json()["reference"] == ref


def test_get_non_existent_resource(live_server):
    base_url = live_server
    response = requests.get(f"{base_url}/rentals/99999999")

    assert response.status_code == 404
    error_data = response.json()
    assert error_data["code"] == "NOT_FOUND"
    assert error_data["message"] == "Оренду не знайдено"


def test_action_endpoint_success(live_server):
    base_url = live_server
    eq_id = create_unique_equipment(base_url)
    ref = f"REF-{uuid.uuid4().hex[:8]}"

    create_resp = requests.post(f"{base_url}/rentals", json={
        "reference": ref, "equipment_id": eq_id, "quantity": 1, "days": 1, "premium": False
    })
    rental_id = create_resp.json()["id"]

    action_resp = requests.post(f"{base_url}/rentals/{rental_id}/return", json={"overdue_days": 1})

    assert action_resp.status_code == 200
    assert action_resp.json()["status"] == "Returned"


def test_action_conflict(live_server):
    base_url = live_server
    eq_id = create_unique_equipment(base_url)
    ref = f"REF-{uuid.uuid4().hex[:8]}"

    create_resp = requests.post(f"{base_url}/rentals", json={
        "reference": ref, "equipment_id": eq_id, "quantity": 1, "days": 1, "premium": False
    })
    rental_id = create_resp.json()["id"]

    requests.post(f"{base_url}/rentals/{rental_id}/return", json={"overdue_days": 0})

    conflict_resp = requests.post(f"{base_url}/rentals/{rental_id}/return", json={"overdue_days": 5})
    assert conflict_resp.status_code == 409
    assert conflict_resp.json()["code"] == "RENTAL_CONFLICT"

    get_resp = requests.get(f"{base_url}/rentals/{rental_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["late_fee"] == 0


@pytest.mark.parametrize("qty, days, premium, expected_status", [
    (2, 2, False, 201),
    (2, 3, False, 409),
    (2, 3, True, 201),
    (2, 11, True, 409)
])
def test_business_rule_parameterized(live_server, qty, days, premium, expected_status):
    base_url = live_server
    eq_id = create_unique_equipment(base_url, daily_rate=50000)
    ref = f"REF-{uuid.uuid4().hex[:8]}"

    response = requests.post(f"{base_url}/rentals", json={
        "reference": ref,
        "equipment_id": eq_id,
        "quantity": qty,
        "days": days,
        "premium": premium
    })

    assert response.status_code == expected_status
    if expected_status == 409:
        assert response.json()["code"] == "RENTAL_CONFLICT"