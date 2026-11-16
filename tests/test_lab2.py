import json
import pytest
import time
import psycopg
import requests
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from pathlib import Path
from rental.models import Equipment, RentalRequest, RentalStatus
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.http_rental_policy import HttpRentalPolicy, RentalPolicyError
from rental.service import RentalService, RentalRejectedError


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:17-alpine") as postgres:
        conn_str = f"postgresql://{postgres.username}:{postgres.password}@{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}/{postgres.dbname}"

        migration_path = Path(__file__).parent.parent / "migrations" / "V1__create_tables.sql"

        with open(migration_path, "r", encoding="utf-8") as f:
            migration_sql = f.read()

        with psycopg.connect(conn_str, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(migration_sql)

        yield conn_str


@pytest.fixture(scope="session")
def wiremock_container():
    with DockerContainer("wiremock/wiremock:latest").with_exposed_ports(8080) as wiremock:
        port = wiremock.get_exposed_port(8080)
        host = wiremock.get_container_host_ip()
        wiremock_url = f"http://{host}:{port}"
        for _ in range(30):
            try:
                response = requests.get(f"{wiremock_url}/__admin/mappings", timeout=1.0)
                if response.status_code == 200:
                    break
            except requests.exceptions.RequestException:
                time.sleep(0.5)
        else:
            raise RuntimeError("WireMock не встиг запуститися за відведений час")

        yield wiremock_url

@pytest.fixture
def db_connection(postgres_container):
    with psycopg.connect(postgres_container, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("TRUNCATE rentals, equipment RESTART IDENTITY CASCADE;")
        yield conn


@pytest.fixture
def wiremock_url(wiremock_container):
    url = wiremock_container
    requests.post(f"{url}/__admin/mappings/reset")
    requests.post(f"{url}/__admin/requests/reset")
    return url


@pytest.fixture
def environment(db_connection, wiremock_url):
    equipment_repo = PostgresEquipmentRepository(db_connection)
    rental_repo = PostgresRentalRepository(db_connection)
    policy = HttpRentalPolicy(base_url=wiremock_url, timeout=2.0)
    service = RentalService(equipment_repo, rental_repo, policy)

    return {
        "equipment_repo": equipment_repo,
        "rental_repo": rental_repo,
        "service": service,
        "wiremock_url": wiremock_url
    }


def stub_wiremock(wiremock_url, status_code, body_dict=None):
    mapping = {
        "request": {"method": "POST", "url": "/rental-policy"},
        "response": {"status": status_code, "headers": {"Content-Type": "application/json"}}
    }
    if body_dict is not None:
        mapping["response"]["jsonBody"] = body_dict

    response = requests.post(f"{wiremock_url}/__admin/mappings", json=mapping)
    response.raise_for_status()


def get_wiremock_requests(wiremock_url):
    response = requests.get(f"{wiremock_url}/__admin/requests")
    return response.json()["requests"]


def test_positive(environment):
    env = environment
    stub_wiremock(env["wiremock_url"], status_code=200, body_dict={"allowed": True})

    eq = env["equipment_repo"].create(Equipment(id=0, code="DRILL-1", daily_rate=150, stock=10))
    req = RentalRequest(reference="REF-001", equipment_id=eq.id, quantity=5, days=2, premium=False)

    order = env["service"].rent(req)

    persisted_order = env["rental_repo"].find_by_id(order.id)
    assert persisted_order is not None
    assert persisted_order.status == RentalStatus.ACTIVE
    assert persisted_order.reference == "REF-001"
    assert persisted_order.total == 1500

    requests_log = get_wiremock_requests(env["wiremock_url"])
    assert len(requests_log) == 1
    logged_request = requests_log[0]["request"]
    assert logged_request["method"] == "POST"
    assert logged_request["url"] == "/rental-policy"

    body = json.loads(logged_request["body"])
    assert body["equipmentId"] == eq.id
    assert body["quantity"] == 5
    assert body["days"] == 2


def test_external_rejection(environment):
    env = environment
    stub_wiremock(env["wiremock_url"], status_code=200, body_dict={"allowed": False})

    eq = env["equipment_repo"].create(Equipment(id=0, code="DRILL-2", daily_rate=150, stock=10))
    req = RentalRequest(reference="REF-002", equipment_id=eq.id, quantity=5, days=2, premium=False)

    with pytest.raises(RentalRejectedError, match="Оренду не дозволено"):
        env["service"].rent(req)

    rentals = env["rental_repo"].find_rentals(eq.id, RentalStatus.ACTIVE)
    assert len(rentals) == 0

    requests_log = get_wiremock_requests(env["wiremock_url"])
    assert len(requests_log) == 1
    assert json.loads(requests_log[0]["request"]["body"])["equipmentId"] == eq.id


def test_external_service_unavailable(environment):
    env = environment
    stub_wiremock(env["wiremock_url"], status_code=503)

    eq = env["equipment_repo"].create(Equipment(id=0, code="DRILL-3", daily_rate=150, stock=10))
    req = RentalRequest(reference="REF-003", equipment_id=eq.id, quantity=5, days=2, premium=False)

    with pytest.raises(RentalPolicyError):
        env["service"].rent(req)

    assert len(env["rental_repo"].find_rentals(eq.id, RentalStatus.ACTIVE)) == 0

    assert len(get_wiremock_requests(env["wiremock_url"])) == 1


def test_local_rejection(environment):
    env = environment
    stub_wiremock(env["wiremock_url"], status_code=200, body_dict={"allowed": True})

    eq = env["equipment_repo"].create(Equipment(id=0, code="TRACTOR-1", daily_rate=50000, stock=10))
    req = RentalRequest(reference="REF-004", equipment_id=eq.id, quantity=5, days=1, premium=False)

    with pytest.raises(RentalRejectedError, match="Персональні умови оренди не виконані"):
        env["service"].rent(req)

    assert len(env["rental_repo"].find_rentals(eq.id, RentalStatus.ACTIVE)) == 0

    requests_log = get_wiremock_requests(env["wiremock_url"])
    assert len(requests_log) == 0