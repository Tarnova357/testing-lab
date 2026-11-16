from unittest.mock import Mock
import pytest

from rental.models import Equipment, RentalOrder, RentalRequest, RentalStatus
from rental.ports import EquipmentRepository, RentalPolicy, RentalRepository
from rental.service import RentalRejectedError, RentalService, ResourceNotFoundError


def test_calculate_total_unit():
    service = RentalService(Mock(spec=EquipmentRepository), Mock(spec=RentalRepository), Mock(spec=RentalPolicy))
    assert service.calculate_total(daily_rate=10000, quantity=2, days=3, premium=False) == 60000


def test_is_rental_allowed_personal_rule_unit():
    service = RentalService(Mock(spec=EquipmentRepository), Mock(spec=RentalRepository), Mock(spec=RentalPolicy))
    # До 2000 грн (200 000 коп.) включно — дозволено всім
    assert service.is_rental_allowed(daily_rate=10000, quantity=2, days=10, premium=False) is True
    # Понад 2000 грн без преміуму — заборонено
    assert service.is_rental_allowed(daily_rate=25000, quantity=1, days=9, premium=False) is False
    # Понад 2000 грн із преміумом до 10 днів включно — дозволено
    assert service.is_rental_allowed(daily_rate=25000, quantity=1, days=10, premium=True) is True
    # Понад 2000 грн із преміумом понад 10 днів — заборонено
    assert service.is_rental_allowed(daily_rate=25000, quantity=1, days=11, premium=True) is False


def test_rent_unit_with_mocks():
    eq_repo = Mock(spec=EquipmentRepository)
    rent_repo = Mock(spec=RentalRepository)
    policy = Mock(spec=RentalPolicy)

    eq_repo.find_by_id.return_value = Equipment(id=1, code="UNIT-EQ", daily_rate=5000, stock=4)
    policy.is_allowed.return_value = True
    rent_repo.create.side_effect = lambda order: RentalOrder(
        id=42,
        reference=order.reference,
        days=order.days,
        premium=order.premium,
        total=order.total,
        late_fee=order.late_fee,
        status=order.status,
        item=order.item,
    )

    service = RentalService(eq_repo, rent_repo, policy)
    result = service.rent(RentalRequest(reference="UNIT-REF-1", equipment_id=1, quantity=2, days=2, premium=False))

    assert result.id == 42
    assert result.total == 20000
    assert result.status == RentalStatus.ACTIVE
    policy.is_allowed.assert_called_once()


def test_rent_not_found_and_stock_errors_unit():
    eq_repo = Mock(spec=EquipmentRepository)
    service = RentalService(eq_repo, Mock(spec=RentalRepository), Mock(spec=RentalPolicy))

    eq_repo.find_by_id.return_value = None
    with pytest.raises(ResourceNotFoundError):
        service.rent(RentalRequest(reference="UNIT-404", equipment_id=99, quantity=1, days=1))

    eq_repo.find_by_id.return_value = Equipment(id=1, code="UNIT-EQ", daily_rate=5000, stock=1)
    with pytest.raises(RentalRejectedError):
        service.rent(RentalRequest(reference="UNIT-STOCK", equipment_id=1, quantity=3, days=1))