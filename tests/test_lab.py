from unittest.mock import Mock
import pytest

from rental.database import connect
from rental.models import Equipment, RentalItem, RentalOrder, RentalRequest, RentalStatus
from rental.ports import RentalPolicy
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.service import RentalRejectedError, RentalService


@pytest.fixture(scope="session")
def db_connection():
    with connect() as conn:
        yield conn


@pytest.fixture(autouse=True)
def clean_database(db_connection):
    with db_connection.cursor() as cursor:
        cursor.execute("TRUNCATE rentals, equipment RESTART IDENTITY CASCADE;")
    yield


@pytest.fixture
def equipment_repo(db_connection):
    return PostgresEquipmentRepository(db_connection)


@pytest.fixture
def rental_repo(db_connection):
    return PostgresRentalRepository(db_connection)


@pytest.fixture
def mock_policy():
    policy = Mock(spec=RentalPolicy)
    policy.is_allowed.return_value = True
    return policy


@pytest.fixture
def service(equipment_repo, rental_repo, mock_policy):
    return RentalService(equipment_repo, rental_repo, mock_policy)

def test_equipment_create_repositoryState(equipment_repo):
    item = Equipment(id=0, code="DRILL-01", daily_rate=5000, stock=10)
    saved = equipment_repo.create(item)

    assert saved.id > 0

    loaded = equipment_repo.find_by_id(saved.id)
    assert loaded is not None
    assert loaded.id == saved.id
    assert loaded.code == "DRILL-01"
    assert loaded.daily_rate == 5000
    assert loaded.stock == 10


def test_rental_create_repositoryState(equipment_repo, rental_repo):
    eq = equipment_repo.create(Equipment(0, "GENERATOR-01", 15000, 5))

    order = RentalOrder(
        id=0,
        reference="REF-INIT-01",
        days=3,
        premium=False,
        total=45000,
        late_fee=0,
        status=RentalStatus.ACTIVE,
        item=RentalItem(id=0, equipment_id=eq.id, quantity=1, daily_rate=15000),
    )
    saved = rental_repo.create(order)

    found = rental_repo.find_by_id(saved.id)
    assert found is not None
    assert found.reference == "REF-INIT-01"
    assert found.total == 45000
    assert found.status == RentalStatus.ACTIVE
    assert found.item.equipment_id == eq.id
    assert found.item.quantity == 1


def test_equipment_update_and_delete(equipment_repo):
    eq = equipment_repo.create(Equipment(0, "SAW-01", 3000, 8))

    updated_eq = Equipment(id=eq.id, code="SAW-01-V2", daily_rate=3500, stock=6)
    equipment_repo.update(updated_eq)

    loaded = equipment_repo.find_by_id(eq.id)
    assert loaded.code == "SAW-01-V2"
    assert loaded.daily_rate == 3500
    assert loaded.stock == 6

    equipment_repo.delete(eq.id)
    assert equipment_repo.find_by_id(eq.id) is None


def test_rent_serviceState(service, equipment_repo, rental_repo, mock_policy):
    eq = equipment_repo.create(Equipment(0, "CRANE-01", 10000, 3))
    req = RentalRequest(reference="ORD-SUCCESS-01", equipment_id=eq.id, quantity=2, days=5, premium=False)

    created = service.rent(req)

    mock_policy.is_allowed.assert_called_once_with(eq, 2, 5)

    persisted = rental_repo.find_by_id(created.id)
    assert persisted is not None
    assert persisted.status == RentalStatus.ACTIVE
    assert persisted.total == 100000
    assert persisted.item.equipment_id == eq.id
    assert persisted.item.quantity == 2
    assert persisted.item.daily_rate == 10000


def test_rent_policy_rejected_serviceState(service, equipment_repo, rental_repo, mock_policy):
    mock_policy.is_allowed.return_value = False
    eq = equipment_repo.create(Equipment(0, "SCAFF-01", 5000, 10))
    req = RentalRequest(reference="ORD-REJECT-01", equipment_id=eq.id, quantity=1, days=4, premium=False)

    with pytest.raises(RentalRejectedError, match="Оренду не дозволено"):
        service.rent(req)

    active_rentals = rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE)
    assert len(active_rentals) == 0


def test_rent_insufficient_stock(service, equipment_repo, rental_repo, mock_policy):
    eq = equipment_repo.create(Equipment(0, "PUMP-01", 2000, 2))
    req = RentalRequest(reference="ORD-NO-STOCK", equipment_id=eq.id, quantity=5, days=3, premium=False)

    with pytest.raises(RentalRejectedError, match="Недостатньо обладнання"):
        service.rent(req)

    mock_policy.is_allowed.assert_not_called()
    assert len(rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE)) == 0


def test_return_rental(service, equipment_repo, rental_repo):
    eq = equipment_repo.create(Equipment(0, "JACK-01", 4000, 5))
    created = service.rent(RentalRequest("ORD-RETURN-01", eq.id, 1, 2, premium=False))

    result = service.return_rental(created.id, overdue_days=0)
    assert result.status == RentalStatus.RETURNED

    persisted = rental_repo.find_by_id(created.id)
    assert persisted.status == RentalStatus.RETURNED
    assert persisted.late_fee == 0


def test_return_already_returned_rental(service, equipment_repo, rental_repo):
    eq = equipment_repo.create(Equipment(0, "MIXER-01", 3000, 4))
    created = service.rent(RentalRequest("ORD-DOUBLE-RET", eq.id, 1, 2, premium=False))

    service.return_rental(created.id, overdue_days=0)

    with pytest.raises(RentalRejectedError, match="Оренду вже повернено"):
        service.return_rental(created.id, overdue_days=0)

    persisted = rental_repo.find_by_id(created.id)
    assert persisted.status == RentalStatus.RETURNED



def test_non_premium_over_limit(service, equipment_repo, rental_repo):

    eq = equipment_repo.create(Equipment(0, "TRACTOR-01", 25000, 2))
    req = RentalRequest("ORD-OVER-REGULAR", eq.id, quantity=1, days=9, premium=False)

    with pytest.raises(RentalRejectedError, match="Персональні умови оренди не виконані"):
        service.rent(req)

    assert len(rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE)) == 0


def test_premium_over_limit_within_10_days(service, equipment_repo, rental_repo):

    eq = equipment_repo.create(Equipment(0, "TRACTOR-02", 25000, 2))
    req = RentalRequest("ORD-OVER-PREM-OK", eq.id, quantity=1, days=10, premium=True)

    created = service.rent(req)

    persisted = rental_repo.find_by_id(created.id)
    assert persisted is not None
    assert persisted.premium is True
    assert persisted.total == 250000
    assert persisted.status == RentalStatus.ACTIVE


def test_premium_over_limit_exceeding_10_days(service, equipment_repo, rental_repo):
    eq = equipment_repo.create(Equipment(0, "TRACTOR-03", 25000, 2))
    req = RentalRequest("ORD-OVER-PREM-FAIL", eq.id, quantity=1, days=11, premium=True)

    with pytest.raises(RentalRejectedError, match="Персональні умови оренди не виконані"):
        service.rent(req)

    assert len(rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE)) == 0