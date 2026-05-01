from datetime import date, datetime
from decimal import Decimal
from io import StringIO

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.contrib.messages import get_messages
from django.contrib.auth.models import AnonymousUser
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import UserProfile
from users.models import User
from locations.models import Location

from .forms import AssetForm
from .models import Asset, AssetChangeRequest
from .services import (
    approve_asset_change_request,
    deserialize_asset_payload_for_form,
    reject_asset_change_request,
    serialize_asset_form_payload,
    user_requires_asset_change_approval,
)
from .views import AssetChangeRequestListView, _user_can_review_asset_changes


class UserRequiresAssetChangeApprovalTests(TestCase):
    def test_superuser_does_not_require_approval(self):
        user = User.objects.create_superuser(
            username="approval-superuser",
            email="approval-superuser@example.com",
            password="test-pass-123",
        )

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_admin_role_with_approval_enabled_requires_approval(self):
        user = User.objects.create_user(username="approval-admin", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])

        self.assertTrue(user_requires_asset_change_approval(user))

    def test_admin_role_with_approval_disabled_does_not_require_approval(self):
        user = User.objects.create_user(username="approval-admin-disabled", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = False
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_approver_does_not_require_approval(self):
        user = User.objects.create_user(username="approval-approver", password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["can_approve_asset_changes", "asset_changes_require_approval"])

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_user_with_approval_disabled_does_not_require_approval(self):
        user = User.objects.create_user(username="approval-disabled", password="test-pass-123")

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_user_with_approval_enabled_requires_approval(self):
        user = User.objects.create_user(username="approval-enabled", password="test-pass-123")
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])

        self.assertTrue(user_requires_asset_change_approval(user))

    def test_user_without_profile_requires_approval(self):
        user = User.objects.create_user(username="approval-no-profile", password="test-pass-123")
        user.profile.delete()

        self.assertTrue(user_requires_asset_change_approval(user))

    def test_anonymous_user_requires_approval(self):
        self.assertTrue(user_requires_asset_change_approval(AnonymousUser()))


class UserCanReviewAssetChangesTests(TestCase):
    def test_superuser_can_review(self):
        user = User.objects.create_superuser(
            username="review-superuser",
            email="review-superuser@example.com",
            password="test-pass-123",
        )

        self.assertTrue(_user_can_review_asset_changes(user))

    def test_approver_can_review(self):
        user = User.objects.create_user(username="review-approver", password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.save(update_fields=["can_approve_asset_changes"])

        self.assertTrue(_user_can_review_asset_changes(user))

    def test_admin_role_without_approver_flag_cannot_review(self):
        user = User.objects.create_user(username="review-admin", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.save(update_fields=["role"])

        self.assertFalse(_user_can_review_asset_changes(user))


class SerializeAssetFormPayloadTests(TestCase):
    def test_preserves_json_primitive_values(self):
        payload = serialize_asset_form_payload(
            {
                "name": "Laptop",
                "is_active": True,
                "optional": None,
            }
        )

        self.assertEqual(payload["name"], "Laptop")
        self.assertIs(payload["is_active"], True)
        self.assertIsNone(payload["optional"])

    def test_serializes_decimal_to_string(self):
        payload = serialize_asset_form_payload({"purchase_value": Decimal("1234.50")})

        self.assertEqual(payload["purchase_value"], "1234.50")

    def test_serializes_date_to_iso_string(self):
        payload = serialize_asset_form_payload({"purchase_date": date(2026, 4, 27)})

        self.assertEqual(payload["purchase_date"], "2026-04-27")

    def test_serializes_datetime_to_iso_string(self):
        payload = serialize_asset_form_payload({"reviewed_at": datetime(2026, 4, 27, 12, 30, 15)})

        self.assertEqual(payload["reviewed_at"], "2026-04-27T12:30:15")

    def test_serializes_model_instance_to_pk(self):
        user = User.objects.create_user(username="payload-user", password="test-pass-123")

        payload = serialize_asset_form_payload({"responsible_person": user})

        self.assertEqual(payload["responsible_person"], user.pk)

    def test_serializes_mixed_list_values(self):
        user = User.objects.create_user(username="payload-list-user", password="test-pass-123")

        payload = serialize_asset_form_payload(
            {
                "values": [
                    "text",
                    Decimal("10.25"),
                    date(2026, 1, 2),
                    user,
                    None,
                ]
            }
        )

        self.assertEqual(payload["values"], ["text", "10.25", "2026-01-02", user.pk, None])

    def test_serializes_mixed_dict_values(self):
        user = User.objects.create_user(username="payload-dict-user", password="test-pass-123")

        payload = serialize_asset_form_payload(
            {
                "nested": {
                    "amount": Decimal("99.99"),
                    "date": date(2026, 2, 3),
                    "user": user,
                    "flag": False,
                }
            }
        )

        self.assertEqual(
            payload["nested"],
            {
                "amount": "99.99",
                "date": "2026-02-03",
                "user": user.pk,
                "flag": False,
            },
        )

    def test_falls_back_to_string_for_unusual_values(self):
        class UnusualValue:
            def __str__(self):
                return "unusual-value"

        payload = serialize_asset_form_payload({"custom": UnusualValue()})

        self.assertEqual(payload["custom"], "unusual-value")


class DeserializeAssetPayloadForFormTests(TestCase):
    def test_keeps_only_asset_form_fields(self):
        payload = {
            "name": "Laptop",
            "inventory_number": "DESERIALIZE-001",
            "malicious_field": "ignored",
            "id": 123,
            "created_at": "2026-04-27T12:00:00",
            "updated_at": "2026-04-27T12:30:00",
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data, {"name": "Laptop", "inventory_number": "DESERIALIZE-001"})

    def test_preserves_form_compatible_scalar_values(self):
        user = User.objects.create_user(username="deserialize-user", password="test-pass-123")
        payload = {
            "purchase_value": "1234.50",
            "purchase_date": "2026-04-27",
            "responsible_person": user.pk,
            "current_user": user.pk,
            "is_active": True,
            "category": None,
            "manufacturer": "",
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data["purchase_value"], "1234.50")
        self.assertEqual(form_data["purchase_date"], "2026-04-27")
        self.assertEqual(form_data["responsible_person"], user.pk)
        self.assertEqual(form_data["current_user"], user.pk)
        self.assertIs(form_data["is_active"], True)
        self.assertIsNone(form_data["category"])
        self.assertEqual(form_data["manufacturer"], "")

    def test_ignores_nested_values_outside_asset_form_fields(self):
        payload = {
            "name": "Laptop",
            "metadata": {"unexpected": True},
            "tags": ["unexpected"],
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data, {"name": "Laptop"})

    def test_does_not_create_asset(self):
        payload = {
            "name": "Not Created",
            "inventory_number": "DESERIALIZE-NO-CREATE-001",
            "asset_type": Asset.AssetType.FIXED_ASSET,
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data["inventory_number"], "DESERIALIZE-NO-CREATE-001")
        self.assertFalse(Asset.objects.filter(inventory_number="DESERIALIZE-NO-CREATE-001").exists())

    def test_handles_flat_create_payload(self):
        payload = {
            "name": "Create Payload",
            "inventory_number": "DESERIALIZE-CREATE-001",
            "asset_type": Asset.AssetType.IT_EQUIPMENT,
            "location": "Warehouse",
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "review_comment": "ignored",
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data["name"], "Create Payload")
        self.assertEqual(form_data["inventory_number"], "DESERIALIZE-CREATE-001")
        self.assertEqual(form_data["asset_type"], Asset.AssetType.IT_EQUIPMENT)
        self.assertEqual(form_data["location"], "Warehouse")
        self.assertNotIn("review_comment", form_data)

    def test_handles_update_proposed_payload(self):
        update_payload = {
            "current": {
                "name": "Old Name",
                "inventory_number": "DESERIALIZE-UPDATE-001",
            },
            "proposed": {
                "name": "New Name",
                "inventory_number": "DESERIALIZE-UPDATE-001",
                "location": "Updated location",
                "unexpected": "ignored",
            },
        }

        form_data = deserialize_asset_payload_for_form(update_payload["proposed"])

        self.assertEqual(form_data["name"], "New Name")
        self.assertEqual(form_data["inventory_number"], "DESERIALIZE-UPDATE-001")
        self.assertEqual(form_data["location"], "Updated location")
        self.assertNotIn("unexpected", form_data)


class ApproveAssetChangeRequestCreateTests(TestCase):
    def _create_payload(self, inventory_number="APPROVE-CREATE-001", **overrides):
        payload = {
            "name": "Approved Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED_ASSET,
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "category": "IT",
            "location": "Legacy location",
            "is_active": True,
        }
        payload.update(overrides)
        return payload

    def _create_request(self, requested_by, payload=None, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": AssetChangeRequest.Operation.CREATE,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": payload or self._create_payload(),
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_superuser_can_approve_pending_create_and_create_asset(self):
        requester = User.objects.create_user(username="approve-create-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-superuser",
            email="approve-create-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(requester)

        asset = approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(asset.inventory_number, "APPROVE-CREATE-001")
        self.assertEqual(asset.name, "Approved Asset")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertIsNotNone(change_request.reviewed_at)
        self.assertEqual(change_request.asset, asset)

    def test_admin_role_without_approver_flag_cannot_approve_pending_create(self):
        requester = User.objects.create_user(username="approve-create-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="APPROVE-CREATE-ADMIN-001"),
        )

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-ADMIN-001").exists())

    def test_approver_without_global_access_cannot_approve_create_without_location_fk(self):
        requester = User.objects.create_user(username="approve-create-scope-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-scope-reviewer", password="test-pass-123")
        reviewer.profile.can_approve_asset_changes = True
        reviewer.profile.save(update_fields=["can_approve_asset_changes"])
        change_request = self._create_request(requester)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_regular_user_cannot_approve_create(self):
        requester = User.objects.create_user(username="approve-create-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-regular-reviewer", password="test-pass-123")
        change_request = self._create_request(requester)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_approved_request_cannot_be_approved_again(self):
        requester = User.objects.create_user(username="approve-create-approved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-approved-reviewer",
            email="approve-create-approved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_rejected_request_cannot_be_approved(self):
        requester = User.objects.create_user(username="approve-create-rejected-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-rejected-reviewer",
            email="approve-create-rejected-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.REJECTED,
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertIsNone(change_request.reviewed_by)
        self.assertIsNone(change_request.reviewed_at)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_invalid_payload_does_not_create_asset_or_change_status(self):
        requester = User.objects.create_user(username="approve-create-invalid-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-invalid-reviewer",
            email="approve-create-invalid-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(name="", inventory_number=""),
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertIsNone(change_request.reviewed_by)
        self.assertFalse(Asset.objects.exists())

    def test_update_operation_is_not_supported_yet(self):
        requester = User.objects.create_user(username="approve-update-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-reviewer",
            email="approve-update-reviewer@example.com",
            password="test-pass-123",
        )
        asset = Asset.objects.create(
            name="Existing Asset",
            inventory_number="APPROVE-UPDATE-EXISTING-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        change_request = AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": "Existing Asset"}, "proposed": {"name": "Updated Asset"}},
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        asset.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(asset.name, "Existing Asset")

    def test_payload_is_validated_through_asset_form_and_extra_fields_are_ignored(self):
        requester = User.objects.create_user(username="approve-create-extra-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-extra-reviewer",
            email="approve-create-extra-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(
                inventory_number="APPROVE-CREATE-EXTRA-001",
                malicious_field="ignored",
                id=999,
                created_at="2026-04-27T12:00:00",
            ),
        )

        asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(asset.inventory_number, "APPROVE-CREATE-EXTRA-001")
        self.assertNotEqual(asset.pk, 999)
        self.assertFalse(hasattr(asset, "malicious_field"))

    def test_unsaved_change_request_fails_with_controlled_error(self):
        requester = User.objects.create_user(username="approve-create-unsaved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-unsaved-reviewer",
            email="approve-create-unsaved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = AssetChangeRequest(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.CREATE,
            status=AssetChangeRequest.Status.PENDING,
            payload=self._create_payload(),
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        self.assertFalse(Asset.objects.exists())


class ApproveAssetChangeRequestUpdateTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Approve Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Approve Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number="APPROVE-UPDATE-001", location_obj=None, **overrides):
        defaults = {
            "name": "Original Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED_ASSET,
            "category": "IT",
            "location": location_obj.path if location_obj else "Legacy location",
            "location_fk": location_obj,
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "is_active": True,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _current_payload(self, asset):
        values = {
            "name": asset.name,
            "inventory_number": asset.inventory_number,
            "asset_type": asset.asset_type,
            "category": asset.category,
            "manufacturer": asset.manufacturer,
            "model": asset.model,
            "serial_number": asset.serial_number,
            "barcode": asset.barcode,
            "description": asset.description,
            "purchase_date": asset.purchase_date,
            "commissioning_date": asset.commissioning_date,
            "purchase_value": asset.purchase_value,
            "invoice_number": asset.invoice_number,
            "external_id": asset.external_id,
            "cost_center": asset.cost_center,
            "organizational_unit": asset.organizational_unit,
            "department": asset.department,
            "location": asset.location,
            "room": asset.room,
            "responsible_person": asset.responsible_person,
            "current_user": asset.current_user,
            "status": asset.status,
            "technical_condition": asset.technical_condition,
            "last_inventory_date": asset.last_inventory_date,
            "next_review_date": asset.next_review_date,
            "warranty_until": asset.warranty_until,
            "insurance_until": asset.insurance_until,
            "is_active": asset.is_active,
        }
        return serialize_asset_form_payload(values)

    def _proposed_payload(self, asset, **overrides):
        payload = self._current_payload(asset)
        payload.update(
            {
                "name": "Approved Update",
                "asset_type": Asset.AssetType.IT_EQUIPMENT,
                "status": Asset.Status.IN_USE,
                "technical_condition": Asset.TechnicalCondition.VERY_GOOD,
            }
        )
        payload.update(overrides)
        return payload

    def _update_request(self, requester, asset, payload=None, **overrides):
        defaults = {
            "requested_by": requester,
            "operation": AssetChangeRequest.Operation.UPDATE,
            "status": AssetChangeRequest.Status.PENDING,
            "asset": asset,
            "payload": payload or {
                "current": self._current_payload(asset),
                "proposed": self._proposed_payload(asset),
            },
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def _approver_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.save(update_fields=["can_approve_asset_changes"])
        user.profile.allowed_locations.add(location)
        return user

    def test_superuser_can_approve_update(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-super-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-super",
            email="approve-update-super@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(location_obj=location)
        change_request = self._update_request(requester, asset)

        updated_asset = approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(updated_asset.name, "Approved Update")
        self.assertEqual(updated_asset.status, Asset.Status.IN_USE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertIsNotNone(change_request.reviewed_at)

    def test_admin_role_without_approver_flag_cannot_approve_update(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-update-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-ADMIN-001", location_obj=location)
        change_request = self._update_request(requester, asset)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_scoped_approver_can_approve_update_in_scope(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-scope-requester", password="test-pass-123")
        reviewer = self._approver_with_location("approve-update-scope-reviewer", location)
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-SCOPE-001", location_obj=location)
        change_request = self._update_request(requester, asset)

        updated_asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(updated_asset.name, "Approved Update")
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)

    def test_scoped_approver_cannot_approve_update_outside_scope(self):
        allowed_location, outside_location = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-outside-requester", password="test-pass-123")
        reviewer = self._approver_with_location("approve-update-outside-reviewer", allowed_location)
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-OUTSIDE-001", location_obj=outside_location)
        change_request = self._update_request(requester, asset)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_scoped_approver_cannot_approve_update_without_location_fk(self):
        allowed_location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-nofk-requester", password="test-pass-123")
        reviewer = self._approver_with_location("approve-update-nofk-reviewer", allowed_location)
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-NOFK-001", location_obj=None, location_fk=None)
        change_request = self._update_request(requester, asset)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_regular_user_cannot_approve_update(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-update-regular-reviewer", password="test-pass-123")
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-REGULAR-001", location_obj=location)
        change_request = self._update_request(requester, asset)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_update_request_without_asset_fails(self):
        requester = User.objects.create_user(username="approve-update-noasset-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-noasset-reviewer",
            email="approve-update-noasset-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=None,
            payload={"current": {}, "proposed": {}},
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_update_payload_without_current_fails(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-nocurrent-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-nocurrent-reviewer",
            email="approve-update-nocurrent-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-NOCURRENT-001", location_obj=location)
        change_request = self._update_request(requester, asset, payload={"proposed": self._proposed_payload(asset)})

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")

    def test_update_payload_without_proposed_fails(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-noproposed-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-noproposed-reviewer",
            email="approve-update-noproposed-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-NOPROPOSED-001", location_obj=location)
        change_request = self._update_request(requester, asset, payload={"current": self._current_payload(asset)})

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")

    def test_current_conflict_fails_without_saving(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-conflict-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-conflict-reviewer",
            email="approve-update-conflict-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-CONFLICT-001", location_obj=location)
        change_request = self._update_request(requester, asset)
        asset.name = "Changed Elsewhere"
        asset.save(update_fields=["name"])

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Changed Elsewhere")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_invalid_proposed_payload_fails_without_saving(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-invalid-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-invalid-reviewer",
            email="approve-update-invalid-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-INVALID-001", location_obj=location)
        change_request = self._update_request(
            requester,
            asset,
            payload={
                "current": self._current_payload(asset),
                "proposed": self._proposed_payload(asset, name="", inventory_number=""),
            },
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(asset.inventory_number, "APPROVE-UPDATE-INVALID-001")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_extra_fields_are_ignored_and_legacy_location_is_updated(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-extra-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-extra-reviewer",
            email="approve-update-extra-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-EXTRA-001", location_obj=location)
        proposed = self._proposed_payload(
            asset,
            name="Extra Ignored Update",
            location="LOKALIZACJA SPOZA ZAKRESU",
            malicious_field="ignored",
            id=999,
        )
        change_request = self._update_request(
            requester,
            asset,
            payload={"current": self._current_payload(asset), "proposed": proposed},
        )

        updated_asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(updated_asset.name, "Extra Ignored Update")
        self.assertEqual(updated_asset.location, "LOKALIZACJA SPOZA ZAKRESU")
        self.assertNotEqual(updated_asset.pk, 999)
        self.assertFalse(hasattr(updated_asset, "malicious_field"))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)


class RejectAssetChangeRequestTests(TestCase):
    def _create_payload(self, inventory_number="REJECT-CREATE-001"):
        return {
            "name": "Rejected Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED_ASSET,
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
        }

    def _create_request(self, requested_by, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": AssetChangeRequest.Operation.CREATE,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": self._create_payload(),
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_superuser_can_reject_pending_create(self):
        requester = User.objects.create_user(username="reject-superuser-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-superuser",
            email="reject-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(requester)

        rejected_request = reject_asset_change_request(change_request, reviewer, comment="Not enough data")

        change_request.refresh_from_db()
        self.assertEqual(rejected_request.pk, change_request.pk)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertIsNotNone(change_request.reviewed_at)
        self.assertEqual(change_request.review_comment, "Not enough data")
        self.assertIsNone(change_request.asset)

    def test_admin_role_without_approver_flag_cannot_reject_pending_create(self):
        requester = User.objects.create_user(username="reject-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="reject-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_request(requester)

        with self.assertRaises(PermissionDenied):
            reject_asset_change_request(change_request, reviewer, comment="Rejected by admin")

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.reviewed_by)
        self.assertEqual(change_request.review_comment, "")

    def test_approver_can_reject_pending_create(self):
        requester = User.objects.create_user(username="reject-approver-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="reject-approver", password="test-pass-123")
        reviewer.profile.can_approve_asset_changes = True
        reviewer.profile.save(update_fields=["can_approve_asset_changes"])
        change_request = self._create_request(requester)

        reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertEqual(change_request.review_comment, "")

    def test_regular_user_cannot_reject(self):
        requester = User.objects.create_user(username="reject-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="reject-regular", password="test-pass-123")
        change_request = self._create_request(requester)

        with self.assertRaises(PermissionDenied):
            reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.reviewed_by)

    def test_approved_request_cannot_be_rejected(self):
        requester = User.objects.create_user(username="reject-approved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-approved-reviewer",
            email="reject-approved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
        )

        with self.assertRaises(ValidationError):
            reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)

    def test_rejected_request_cannot_be_rejected_again(self):
        requester = User.objects.create_user(username="reject-rejected-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-rejected-reviewer",
            email="reject-rejected-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.REJECTED,
            reviewed_by=reviewer,
            review_comment="Already rejected",
        )

        with self.assertRaises(ValidationError):
            reject_asset_change_request(change_request, reviewer, comment="Second rejection")

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.review_comment, "Already rejected")

    def test_unsaved_change_request_fails_with_controlled_error(self):
        requester = User.objects.create_user(username="reject-unsaved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-unsaved-reviewer",
            email="reject-unsaved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = AssetChangeRequest(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.CREATE,
            status=AssetChangeRequest.Status.PENDING,
            payload=self._create_payload(),
        )

        with self.assertRaises(ValidationError):
            reject_asset_change_request(change_request, reviewer)

    def test_reject_does_not_create_asset_for_create_request(self):
        requester = User.objects.create_user(username="reject-no-create-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-no-create-reviewer",
            email="reject-no-create-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="REJECT-NO-CREATE-001"),
        )

        reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="REJECT-NO-CREATE-001").exists())

    def test_reject_does_not_change_asset_for_update_request(self):
        requester = User.objects.create_user(username="reject-update-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-update-reviewer",
            email="reject-update-reviewer@example.com",
            password="test-pass-123",
        )
        asset = Asset.objects.create(
            name="Original Asset",
            inventory_number="REJECT-UPDATE-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        change_request = AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": "Original Asset"}, "proposed": {"name": "Rejected Update"}},
        )

        reject_asset_change_request(change_request, reviewer, comment="No update")

        change_request.refresh_from_db()
        asset.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.asset, asset)
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.review_comment, "No update")


class AssetChangeRequestModelTests(TestCase):
    def test_can_create_request_without_asset(self):
        user = User.objects.create_user(username="requester-create", password="test-pass-123")

        request = AssetChangeRequest.objects.create(
            requested_by=user,
            operation=AssetChangeRequest.Operation.CREATE,
            payload={
                "name": "New Laptop",
                "inventory_number": "NEW-001",
                "attributes": {"manufacturer": "Dell"},
            },
        )

        self.assertIsNone(request.asset)
        self.assertEqual(request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(request.payload["name"], "New Laptop")
        self.assertEqual(request.payload["attributes"]["manufacturer"], "Dell")

    def test_can_create_update_request_with_asset(self):
        user = User.objects.create_user(username="requester-update", password="test-pass-123")
        asset = Asset.objects.create(
            name="Existing Laptop",
            inventory_number="UPD-001",
            status=Asset.Status.IN_STOCK,
            location="HQ",
            category="IT",
        )

        request = AssetChangeRequest.objects.create(
            requested_by=user,
            operation=AssetChangeRequest.Operation.UPDATE,
            asset=asset,
            payload={"status": Asset.Status.IN_USE, "location": "HQ / Room 1"},
        )

        self.assertEqual(request.asset, asset)
        self.assertEqual(request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(request.payload["status"], Asset.Status.IN_USE)
        self.assertEqual(request.payload["location"], "HQ / Room 1")


class AssetChangeRequestListViewTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Queue Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Queue Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number, location):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            status=Asset.Status.IN_STOCK,
            location=location.path if location else "Legacy only",
            location_fk=location,
            category="IT",
        )

    def _create_change_request(self, requested_by, operation, marker, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": operation,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": {"name": marker, "inventory_number": marker},
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_change_list_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_regular_user_sees_only_own_requests(self):
        user = User.objects.create_user(username="queue-regular-user", password="test-pass-123")
        other_user = User.objects.create_user(username="queue-other-user", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Operation.CREATE, "QUEUE-OWN-REQUEST")
        self._create_change_request(other_user, AssetChangeRequest.Operation.CREATE, "QUEUE-OTHER-REQUEST")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-OWN-REQUEST")
        self.assertNotContains(response, "QUEUE-OTHER-REQUEST")

    def test_regular_user_status_filters_apply_to_own_requests(self):
        user = User.objects.create_user(username="queue-regular-filter-user", password="test-pass-123")
        other_user = User.objects.create_user(username="queue-regular-filter-other", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Operation.CREATE, "QUEUE-OWN-PENDING")
        self._create_change_request(
            user,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-OWN-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self._create_change_request(
            other_user,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-OTHER-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.APPROVED})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-OWN-APPROVED")
        self.assertNotContains(response, "QUEUE-OWN-PENDING")
        self.assertNotContains(response, "QUEUE-OTHER-APPROVED")

    def test_superuser_sees_pending_create_and_update_requests(self):
        requester = User.objects.create_user(username="queue-super-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-superuser",
            email="queue-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-SUPER-UPD-001", location)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-SUPER-CREATE-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-SUPER-UPDATE-001",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "QUEUE-SUPER-UPDATE-001"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-SUPER-CREATE-001")
        self.assertContains(response, "QUEUE-SUPER-UPD-001")

    def test_admin_role_without_approver_flag_sees_only_own_change_list(self):
        requester = User.objects.create_user(username="queue-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="queue-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-ADMIN-UPD-001", location)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-ADMIN-CREATE-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-ADMIN-UPDATE-001",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "QUEUE-ADMIN-UPDATE-001"}},
        )
        self._create_change_request(reviewer, AssetChangeRequest.Operation.CREATE, "QUEUE-ADMIN-OWN-REQUEST")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-ADMIN-OWN-REQUEST")
        self.assertNotContains(response, "QUEUE-ADMIN-CREATE-001")
        self.assertNotContains(response, "QUEUE-ADMIN-UPD-001")

    def test_scoped_approver_sees_only_update_requests_in_scope(self):
        requester = User.objects.create_user(username="queue-scope-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="queue-scope-reviewer", password="test-pass-123")
        reviewer.profile.can_approve_asset_changes = True
        reviewer.profile.save(update_fields=["can_approve_asset_changes"])
        allowed_location, outside_location = self._create_location_tree()
        reviewer.profile.allowed_locations.add(allowed_location)
        in_scope_asset = self._create_asset("QUEUE-IN-SCOPE-001", allowed_location)
        outside_asset = self._create_asset("QUEUE-OUT-SCOPE-001", outside_location)
        null_location_asset = self._create_asset("QUEUE-NO-FK-001", None)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-CREATE-NO-FK-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-IN-SCOPE-UPDATE",
            asset=in_scope_asset,
            payload={"current": {"name": in_scope_asset.name}, "proposed": {"name": "QUEUE-IN-SCOPE-UPDATE"}},
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-OUT-SCOPE-UPDATE",
            asset=outside_asset,
            payload={"current": {"name": outside_asset.name}, "proposed": {"name": "QUEUE-OUT-SCOPE-UPDATE"}},
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-NO-FK-UPDATE",
            asset=null_location_asset,
            payload={"current": {"name": null_location_asset.name}, "proposed": {"name": "QUEUE-NO-FK-UPDATE"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-IN-SCOPE-001")
        self.assertNotContains(response, "QUEUE-OUT-SCOPE-001")
        self.assertNotContains(response, "QUEUE-NO-FK-001")
        self.assertNotContains(response, "QUEUE-CREATE-NO-FK-001")

    def test_default_list_shows_only_pending_requests(self):
        requester = User.objects.create_user(username="queue-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-status-superuser",
            email="queue-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-PENDING-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVED-001",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "QUEUE-PENDING-001")
        self.assertNotContains(response, "QUEUE-APPROVED-001")

    def test_status_filters(self):
        requester = User.objects.create_user(username="queue-filter-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-filter-status-superuser",
            email="queue-filter-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-FILTER-PENDING")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-FILTER-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(reviewer)

        approved_response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.APPROVED})
        all_response = self.client.get(reverse("assets:change-list"), {"status": "all"})

        self.assertContains(approved_response, "QUEUE-FILTER-APPROVED")
        self.assertNotContains(approved_response, "QUEUE-FILTER-PENDING")
        self.assertContains(all_response, "QUEUE-FILTER-APPROVED")
        self.assertContains(all_response, "QUEUE-FILTER-PENDING")

    def test_change_list_translates_pending_status(self):
        requester = User.objects.create_user(username="queue-pending-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-pending-status-superuser",
            email="queue-pending-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-PENDING-STATUS")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "<div>Oczekuje</div>", html=True)

    def test_change_list_translates_approved_status(self):
        requester = User.objects.create_user(username="queue-approved-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-approved-status-superuser",
            email="queue-approved-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVED-STATUS",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.APPROVED})

        self.assertContains(response, "<div>Zatwierdzone</div>", html=True)

    def test_change_list_translates_rejected_status(self):
        requester = User.objects.create_user(username="queue-rejected-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-rejected-status-superuser",
            email="queue-rejected-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECTED-STATUS",
            status=AssetChangeRequest.Status.REJECTED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.REJECTED})

        self.assertContains(response, "<div>Odrzucone</div>", html=True)

    def test_change_list_shows_rejected_comment(self):
        requester = User.objects.create_user(username="queue-rejected-comment-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-rejected-comment-superuser",
            email="queue-rejected-comment-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECTED-COMMENT",
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="Błędna wartość",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.REJECTED})

        self.assertContains(response, "<div>Powód: Błędna wartość</div>", html=True)

    def test_change_list_does_not_show_rejected_reason_without_comment(self):
        requester = User.objects.create_user(username="queue-rejected-empty-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-rejected-empty-superuser",
            email="queue-rejected-empty-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECTED-EMPTY",
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.REJECTED})

        self.assertContains(response, "<div>Odrzucone</div>", html=True)
        self.assertNotContains(response, "Powód")

    def test_operation_filters(self):
        requester = User.objects.create_user(username="queue-filter-operation-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-filter-operation-superuser",
            email="queue-filter-operation-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-FILTER-UPD-ASSET", location)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-FILTER-CREATE")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-FILTER-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "QUEUE-FILTER-UPDATE"}},
        )
        self.client.force_login(reviewer)

        create_response = self.client.get(reverse("assets:change-list"), {"operation": AssetChangeRequest.Operation.CREATE})
        update_response = self.client.get(reverse("assets:change-list"), {"operation": AssetChangeRequest.Operation.UPDATE})

        self.assertContains(create_response, "QUEUE-FILTER-CREATE")
        self.assertNotContains(create_response, "QUEUE-FILTER-UPD-ASSET")
        self.assertContains(update_response, "QUEUE-FILTER-UPD-ASSET")
        self.assertNotContains(update_response, "QUEUE-FILTER-CREATE")

    def test_change_list_shows_changes_column_and_hides_review_columns(self):
        requester = User.objects.create_user(username="queue-columns-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-columns-superuser",
            email="queue-columns-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-COLUMNS-CREATE")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<th>Zmiany</th>", html=True)
        self.assertNotContains(response, "<th>Sprawdził</th>", html=True)
        self.assertNotContains(response, "<th>Sprawdzono</th>", html=True)

    def test_change_list_shows_update_changed_fields(self):
        requester = User.objects.create_user(username="queue-diff-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-diff-superuser",
            email="queue-diff-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-DIFF-ASSET", location)
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-DIFF-UPDATE",
            asset=asset,
            payload={
                "current": {"name": "Old name", "inventory_number": "OLD-001"},
                "proposed": {"name": "New name", "inventory_number": "NEW-001"},
            },
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "Nazwa: Old name → New name")
        self.assertContains(response, "Nr inw.: OLD-001 → NEW-001")

    def test_change_list_limits_update_changes_to_three_fields(self):
        requester = User.objects.create_user(username="queue-diff-limit-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-diff-limit-superuser",
            email="queue-diff-limit-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-DIFF-LIMIT-ASSET", location)
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-DIFF-LIMIT-UPDATE",
            asset=asset,
            payload={
                "current": {
                    "name": "Old name",
                    "inventory_number": "OLD-001",
                    "value": 1000,
                    "location": "Old location",
                    "status": "in_stock",
                },
                "proposed": {
                    "name": "New name",
                    "inventory_number": "NEW-001",
                    "value": 1200,
                    "location": "New location",
                    "status": "in_use",
                },
            },
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "Nazwa: Old name → New name")
        self.assertContains(response, "Nr inw.: OLD-001 → NEW-001")
        self.assertContains(response, "Wartość: 1000 → 1200")
        self.assertContains(response, "+ 2 innych zmian")
        self.assertNotContains(response, "Lokalizacja: Old location → New location")
        self.assertNotContains(response, "Status: in_stock → in_use")

    def test_change_list_shows_create_summary(self):
        requester = User.objects.create_user(username="queue-create-summary-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-create-summary-superuser",
            email="queue-create-summary-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-CREATE-SUMMARY")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "Nowy składnik")

    def test_change_list_shows_approve_button_for_pending_request(self):
        requester = User.objects.create_user(username="queue-approve-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-approve-button-superuser",
            email="queue-approve-button-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVE-BUTTON",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        approve_url = reverse("assets:change-approve", kwargs={"pk": change_request.pk})
        self.assertContains(response, "<th>Akcje</th>", html=True)
        self.assertContains(response, f'action="{approve_url}"')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, "Zatwierdź")

    def test_change_list_shows_reject_action_for_pending_request(self):
        requester = User.objects.create_user(username="queue-reject-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-reject-button-superuser",
            email="queue-reject-button-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECT-BUTTON",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        reject_url = reverse("assets:change-reject", kwargs={"pk": change_request.pk})
        self.assertContains(response, "Odrzu")
        self.assertContains(response, f'action="{reject_url}"')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="comment"')
        self.assertContains(response, 'data-role="toggle-reject-form"')
        self.assertContains(response, f'data-target="reject-form-{change_request.pk}"')
        self.assertContains(response, f'id="reject-form-{change_request.pk}"')
        self.assertContains(response, "Potwierd")

    def test_change_list_hides_approve_button_for_non_pending_requests(self):
        requester = User.objects.create_user(username="queue-no-approve-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-no-approve-button-superuser",
            email="queue-no-approve-button-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-APPROVE-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-APPROVE-REJECTED",
            status=AssetChangeRequest.Status.REJECTED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": "all"})

        self.assertContains(response, "QUEUE-NO-APPROVE-APPROVED")
        self.assertContains(response, "QUEUE-NO-APPROVE-REJECTED")
        self.assertNotContains(response, "Zatwierdź")
        self.assertNotContains(response, "change-approve")

    def test_change_list_hides_reject_action_for_non_pending_requests(self):
        requester = User.objects.create_user(username="queue-no-reject-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-no-reject-button-superuser",
            email="queue-no-reject-button-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-REJECT-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-REJECT-REJECTED",
            status=AssetChangeRequest.Status.REJECTED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": "all"})

        self.assertContains(response, "QUEUE-NO-REJECT-APPROVED")
        self.assertContains(response, "QUEUE-NO-REJECT-REJECTED")
        self.assertNotContains(response, "change-reject")
        self.assertNotContains(response, 'name="comment"')

    def test_change_list_approve_post_approves_request(self):
        requester = User.objects.create_user(username="queue-approve-post-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-approve-post-superuser",
            email="queue-approve-post-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVE-POST-001",
            payload={
                "name": "Queue Approve Post",
                "inventory_number": "QUEUE-APPROVE-POST-001",
                "asset_type": Asset.AssetType.IT_EQUIPMENT,
                "category": "IT",
                "status": Asset.Status.IN_STOCK,
                "technical_condition": Asset.TechnicalCondition.GOOD,
                "is_active": True,
            },
        )
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        change_request.refresh_from_db()
        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertTrue(Asset.objects.filter(inventory_number="QUEUE-APPROVE-POST-001").exists())

    def test_change_list_reject_post_rejects_request_with_comment(self):
        requester = User.objects.create_user(username="queue-reject-post-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-reject-post-superuser",
            email="queue-reject-post-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECT-POST-001",
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Rejected from list"},
        )

        change_request.refresh_from_db()
        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.review_comment, "Rejected from list")

    def test_change_list_paginate_by_50(self):
        self.assertEqual(AssetChangeRequestListView.paginate_by, 50)


class AssetChangeRequestDetailViewTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Detail Queue Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Detail Queue Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number, location):
        return Asset.objects.create(
            name=f"Detail Asset {inventory_number}",
            inventory_number=inventory_number,
            status=Asset.Status.IN_STOCK,
            location=location.path if location else "Legacy only",
            location_fk=location,
            category="IT",
        )

    def _create_change_request(self, requested_by, operation, marker, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": operation,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": {"name": marker, "inventory_number": marker},
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def _approver_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.save(update_fields=["can_approve_asset_changes"])
        user.profile.allowed_locations.add(location)
        return user

    def test_change_detail_redirects_anonymous_user_to_login(self):
        requester = User.objects.create_user(username="change-detail-anon-requester", password="test-pass-123")
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CHANGE-ANON-001",
        )

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_regular_user_gets_403_for_change_detail(self):
        requester = User.objects.create_user(username="change-detail-regular-requester", password="test-pass-123")
        user = User.objects.create_user(username="change-detail-regular", password="test-pass-123")
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CHANGE-REGULAR-001",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)

    def test_superuser_sees_create_request_detail(self):
        requester = User.objects.create_user(username="change-detail-super-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-superuser",
            email="change-detail-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CREATE-SUPER-001",
            payload={"name": "Detail Create Asset", "inventory_number": "DETAIL-CREATE-SUPER-001"},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "asset-change-diff-table")
        self.assertContains(response, "Detail Create Asset")
        self.assertContains(response, "DETAIL-CREATE-SUPER-001")

    def test_admin_role_without_approver_flag_gets_403_for_change_detail(self):
        requester = User.objects.create_user(username="change-detail-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="change-detail-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CREATE-ADMIN-001",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)

    def test_scoped_approver_sees_update_request_in_scope(self):
        requester = User.objects.create_user(username="change-detail-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._approver_with_location("change-detail-scope-reviewer", allowed_location)
        asset = self._create_asset("DETAIL-SCOPE-ASSET-001", allowed_location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-SCOPE-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Detail Scope Updated"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Detail Scope Updated")

    def test_scoped_approver_gets_404_for_update_outside_scope(self):
        requester = User.objects.create_user(username="change-detail-outside-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._approver_with_location("change-detail-outside-reviewer", allowed_location)
        asset = self._create_asset("DETAIL-OUTSIDE-ASSET-001", outside_location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-OUTSIDE-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Outside Updated"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)

    def test_scoped_approver_gets_404_for_update_without_location_fk(self):
        requester = User.objects.create_user(username="change-detail-nofk-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._approver_with_location("change-detail-nofk-reviewer", allowed_location)
        asset = self._create_asset("DETAIL-NOFK-ASSET-001", None)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-NOFK-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "No FK Updated"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)

    def test_scoped_approver_gets_404_for_create_without_location_fk(self):
        requester = User.objects.create_user(username="change-detail-create-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._approver_with_location("change-detail-create-scope-reviewer", allowed_location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CREATE-SCOPE-HIDDEN",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)

    def test_update_detail_shows_only_changed_fields(self):
        requester = User.objects.create_user(username="change-detail-diff-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-diff-superuser",
            email="change-detail-diff-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("DETAIL-DIFF-ASSET-001", location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-DIFF-UPDATE",
            asset=asset,
            payload={
                "current": {"name": "Old Name", "status": Asset.Status.IN_STOCK, "category": "Same"},
                "proposed": {"name": "New Name", "status": Asset.Status.IN_USE, "category": "Same"},
            },
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertContains(response, 'class="asset-change-diff-table"')
        self.assertContains(response, "<th>Pole</th>", html=True)
        self.assertContains(response, "<th>Było</th>", html=True)
        self.assertContains(response, "<th>Jest</th>", html=True)
        self.assertContains(response, "Old Name")
        self.assertContains(response, "New Name")
        self.assertContains(response, Asset.Status.IN_STOCK)
        self.assertContains(response, Asset.Status.IN_USE)
        self.assertNotContains(response, "<td>category</td>", html=True)

    def test_update_detail_without_differences_shows_message(self):
        requester = User.objects.create_user(username="change-detail-nodiff-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-nodiff-superuser",
            email="change-detail-nodiff-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("DETAIL-NODIFF-ASSET-001", location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-NODIFF-UPDATE",
            asset=asset,
            payload={"current": {"name": "Same Name"}, "proposed": {"name": "Same Name"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak różnic w payloadzie.")

    def test_broken_update_payload_does_not_break_detail_view(self):
        requester = User.objects.create_user(username="change-detail-broken-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-broken-superuser",
            email="change-detail-broken-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("DETAIL-BROKEN-ASSET-001", location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-BROKEN-UPDATE",
            asset=asset,
            payload={"current": "not-a-dict"},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak różnic w payloadzie.")

    def test_pending_detail_shows_approve_form_with_action_url(self):
        requester = User.objects.create_user(username="change-detail-approve-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-approve-form-superuser",
            email="change-detail-approve-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-APPROVE-FORM-001",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        approve_url = reverse("assets:change-approve", kwargs={"pk": change_request.pk})
        self.assertContains(response, 'class="asset-change-approve-form"')
        self.assertContains(response, f'action="{approve_url}"')
        self.assertContains(response, "Zatwierdź")

    def test_pending_detail_shows_reject_form_with_action_url_and_comment_field(self):
        requester = User.objects.create_user(username="change-detail-reject-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-reject-form-superuser",
            email="change-detail-reject-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-REJECT-FORM-001",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        reject_url = reverse("assets:change-reject", kwargs={"pk": change_request.pk})
        self.assertContains(response, 'class="asset-change-reject-form"')
        self.assertContains(response, f'action="{reject_url}"')
        self.assertContains(response, 'name="comment"')
        self.assertContains(response, "Komentarz do odrzucenia")
        self.assertContains(response, "Odrzuć")

    def test_approved_detail_does_not_show_approval_forms(self):
        requester = User.objects.create_user(username="change-detail-approved-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-approved-form-superuser",
            email="change-detail-approved-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-APPROVED-NO-FORMS-001",
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertNotContains(response, "asset-change-approve-form")
        self.assertNotContains(response, "asset-change-reject-form")
        self.assertNotContains(response, reverse("assets:change-approve", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, reverse("assets:change-reject", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, 'name="comment"')

    def test_rejected_detail_does_not_show_approval_forms(self):
        requester = User.objects.create_user(username="change-detail-rejected-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-rejected-form-superuser",
            email="change-detail-rejected-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-REJECTED-NO-FORMS-001",
            status=AssetChangeRequest.Status.REJECTED,
            reviewed_by=reviewer,
            review_comment="Already rejected",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertNotContains(response, "asset-change-approve-form")
        self.assertNotContains(response, "asset-change-reject-form")
        self.assertNotContains(response, reverse("assets:change-approve", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, reverse("assets:change-reject", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, 'name="comment"')


class AssetChangeRequestPostWorkflowViewTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Post Workflow Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Post Workflow Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _admin_user(self, username):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.save(update_fields=["role"])
        return user

    def _superuser(self, username):
        return User.objects.create_superuser(
            username=username,
            email=f"{username}@example.com",
            password="test-pass-123",
        )

    def _scoped_approver(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.save(update_fields=["can_approve_asset_changes"])
        user.profile.allowed_locations.add(location)
        return user

    def _create_asset(self, inventory_number, location):
        return Asset.objects.create(
            name=f"Post Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=Asset.AssetType.FIXED_ASSET,
            category="IT",
            location=location.path if location else "Legacy only",
            location_fk=location,
            status=Asset.Status.IN_STOCK,
            technical_condition=Asset.TechnicalCondition.GOOD,
            is_active=True,
        )

    def _asset_payload(self, asset):
        return serialize_asset_form_payload(
            {
                field_name: getattr(asset, field_name)
                for field_name in AssetForm.Meta.fields
            }
        )

    def _create_payload(self, inventory_number):
        return {
            "name": f"Post Create {inventory_number}",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED_ASSET,
            "category": "IT",
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "is_active": True,
        }

    def _create_request(self, requester, inventory_number="POST-CREATE-001", **overrides):
        defaults = {
            "requested_by": requester,
            "operation": AssetChangeRequest.Operation.CREATE,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": self._create_payload(inventory_number),
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def _update_request(self, requester, asset, proposed_name="Post Updated Asset", **overrides):
        current = self._asset_payload(asset)
        proposed = current.copy()
        proposed["name"] = proposed_name
        defaults = {
            "requested_by": requester,
            "operation": AssetChangeRequest.Operation.UPDATE,
            "status": AssetChangeRequest.Status.PENDING,
            "asset": asset,
            "payload": {"current": current, "proposed": proposed},
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_superuser_can_approve_create(self):
        requester = User.objects.create_user(username="post-approve-create-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-create-superuser")
        change_request = self._create_request(requester, inventory_number="POST-APPROVE-CREATE-001")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertTrue(Asset.objects.filter(inventory_number="POST-APPROVE-CREATE-001").exists())

    def test_superuser_can_approve_update(self):
        requester = User.objects.create_user(username="post-approve-update-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-update-superuser")
        location, _ = self._create_location_tree()
        asset = self._create_asset("POST-APPROVE-UPDATE-001", location)
        change_request = self._update_request(requester, asset, proposed_name="Post Admin Approved")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Post Admin Approved")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)

    def test_scoped_approver_can_approve_in_scope_update(self):
        requester = User.objects.create_user(username="post-approve-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._scoped_approver("post-approve-scope-reviewer", allowed_location)
        asset = self._create_asset("POST-APPROVE-SCOPE-001", allowed_location)
        change_request = self._update_request(requester, asset, proposed_name="Post Scoped Approved")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Post Scoped Approved")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)

    def test_scoped_approver_cannot_approve_out_of_scope_update(self):
        requester = User.objects.create_user(username="post-approve-outside-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._scoped_approver("post-approve-outside-reviewer", allowed_location)
        asset = self._create_asset("POST-APPROVE-OUTSIDE-001", outside_location)
        change_request = self._update_request(requester, asset, proposed_name="Post Outside Approved")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertNotEqual(asset.name, "Post Outside Approved")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_regular_user_gets_403_for_approve(self):
        requester = User.objects.create_user(username="post-approve-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="post-approve-regular", password="test-pass-123")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_admin_role_without_approver_flag_gets_403_for_approve(self):
        requester = User.objects.create_user(username="post-approve-admin-requester", password="test-pass-123")
        reviewer = self._admin_user("post-approve-admin")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_approve_non_pending_does_not_change_status(self):
        requester = User.objects.create_user(username="post-approve-nonpending-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-nonpending-superuser")
        change_request = self._create_request(
            requester,
            inventory_number="POST-APPROVE-NONPENDING-001",
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="Already rejected",
        )
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertFalse(Asset.objects.filter(inventory_number="POST-APPROVE-NONPENDING-001").exists())

    def test_superuser_can_reject_with_comment(self):
        requester = User.objects.create_user(username="post-reject-admin-requester", password="test-pass-123")
        reviewer = self._superuser("post-reject-superuser")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Needs more data"},
        )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertEqual(change_request.review_comment, "Needs more data")

    def test_scoped_approver_can_reject_in_scope_update(self):
        requester = User.objects.create_user(username="post-reject-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._scoped_approver("post-reject-scope-reviewer", allowed_location)
        asset = self._create_asset("POST-REJECT-SCOPE-001", allowed_location)
        change_request = self._update_request(requester, asset)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Rejected in scope"},
        )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Post Asset POST-REJECT-SCOPE-001")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.review_comment, "Rejected in scope")

    def test_scoped_approver_cannot_reject_out_of_scope_update(self):
        requester = User.objects.create_user(username="post-reject-outside-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._scoped_approver("post-reject-outside-reviewer", allowed_location)
        asset = self._create_asset("POST-REJECT-OUTSIDE-001", outside_location)
        change_request = self._update_request(requester, asset)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Should not save"},
        )

        self.assertEqual(response.status_code, 404)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.review_comment, "")

    def test_regular_user_gets_403_for_reject(self):
        requester = User.objects.create_user(username="post-reject-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="post-reject-regular", password="test-pass-123")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-reject", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_admin_role_without_approver_flag_gets_403_for_reject(self):
        requester = User.objects.create_user(username="post-reject-admin-requester", password="test-pass-123")
        reviewer = self._admin_user("post-reject-admin")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-reject", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_reject_non_pending_does_not_change_status(self):
        requester = User.objects.create_user(username="post-reject-nonpending-requester", password="test-pass-123")
        reviewer = self._superuser("post-reject-nonpending-superuser")
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
            review_comment="Already approved",
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Do not overwrite"},
        )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.review_comment, "Already approved")


class AssetListApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="api-admin",
            email="api-admin@example.com",
            password="test-pass-123",
        )
        for index in range(1, 61):
            Asset.objects.create(
                name=f"Asset {index:03d}",
                inventory_number=f"INV-{index:03d}",
                status=Asset.Status.IN_USE if index % 2 else Asset.Status.IN_STOCK,
                location="HQ" if index <= 30 else "Branch",
                category="IT" if index % 3 else "Furniture",
                purchase_value=Decimal(index * 100),
            )

        cls.target = Asset.objects.create(
            name="Laptop Executive",
            inventory_number="VIP-001",
            status=Asset.Status.RESERVED,
            location="Board Room",
            category="IT",
            purchase_value=Decimal("9999.99"),
            purchase_date=date(2024, 6, 15),
        )

        cls.date_outside = Asset.objects.create(
            name="Archive Router",
            inventory_number="ARC-001",
            status=Asset.Status.RESERVED,
            location="Archive",
            category="IT",
            purchase_value=Decimal("2500"),
            purchase_date=date(2023, 3, 10),
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_api_uses_default_pagination(self):
        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(len(payload["results"]), 50)
        self.assertEqual(payload["pagination"]["page"], 1)
        self.assertEqual(payload["pagination"]["page_size"], 50)
        self.assertEqual(payload["pagination"]["total_items"], 62)
        self.assertTrue(payload["pagination"]["has_next"])

    def test_api_filters_and_searches(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {
                "search": "VIP",
                "status": Asset.Status.RESERVED,
                "location": "Board Room",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["id"], self.target.id)
        self.assertEqual(payload["results"][0]["name"], "Laptop Executive")

    def test_api_supports_sorting_by_value(self):
        response = self.client.get(reverse("assets:api-list"), {"ordering": "-value", "page_size": 5})

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")
        self.assertEqual(payload["filters"]["ordering"], "-purchase_value")

    def test_api_applies_backend_filters_from_query_params(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {
                "filter__status__equals": Asset.Status.RESERVED,
                "filter__purchase_value__gt": "5000",
                "filter__name__contains": "Laptop",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_supports_enum_in_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__status__in": ",".join([Asset.Status.RESERVED, Asset.Status.IN_USE])},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 32)

    def test_api_supports_number_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_value__between": "9900,10000"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_supports_date_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_date__between": "2024-01-01,2024-12-31"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_ignores_incomplete_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_value__between": "1000,"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 62)

    def test_api_ignores_invalid_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_date__between": "2024-12-31,2024-01-01"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 62)

    def test_api_combines_between_with_other_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {
                "filter__purchase_date__between": "2024-01-01,2024-12-31",
                "filter__status__equals": Asset.Status.RESERVED,
                "filter__name__contains": "Laptop",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_exposes_extended_system_columns(self):
        user = User.objects.create_user(username="operator", first_name="Jan", last_name="Kowalski")
        asset = Asset.objects.create(
            name="Laptop Test",
            inventory_number="EXT-001",
            asset_type=Asset.AssetType.IT_EQUIPMENT,
            manufacturer="Dell",
            model="Latitude",
            serial_number="SN-123",
            barcode="5901234567890",
            department="IT",
            organizational_unit="Centrala",
            room="201A",
            responsible_person=user,
            current_user=user,
            technical_condition=Asset.TechnicalCondition.VERY_GOOD,
            invoice_number="FV/2026/001",
            external_id="ERP-001",
            cost_center="MPK-01",
            is_active=True,
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["asset_type_display"], "Sprzęt IT")
        self.assertEqual(row["manufacturer"], "Dell")
        self.assertEqual(row["serial_number"], "SN-123")
        self.assertEqual(row["responsible_person"], "Jan Kowalski")
        self.assertEqual(row["current_user"], "Jan Kowalski")
        self.assertEqual(row["technical_condition_display"], "Bardzo dobry")
        self.assertEqual(row["invoice_number"], "FV/2026/001")
        self.assertEqual(row["external_id"], "ERP-001")
        self.assertEqual(row["cost_center"], "MPK-01")
        self.assertEqual(row["is_active_display"], "Tak")

    def test_api_marks_asset_with_pending_update(self):
        requester = User.objects.create_user(username="pending-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Pending Update Asset",
            inventory_number="PENDING-UPD-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Pending Update Asset Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_pending_update"])

    def test_api_marks_asset_with_rejected_update(self):
        requester = User.objects.create_user(username="rejected-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Rejected Update Asset",
            inventory_number="REJECTED-UPD-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.REJECTED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Rejected Update Asset Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_rejected_update"])

    def test_api_does_not_mark_asset_without_pending_update(self):
        requester = User.objects.create_user(username="approved-update-requester", password="test-pass-123")
        pending_asset = Asset.objects.create(
            name="Other Pending Update Asset",
            inventory_number="PENDING-UPD-OTHER",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        asset = Asset.objects.create(
            name="No Pending Update Asset",
            inventory_number="NO-PENDING-UPD-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=pending_asset,
            payload={"current": {"name": pending_asset.name}, "proposed": {"name": "Other Edited"}},
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.APPROVED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Approved Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertFalse(row["has_pending_update"])
        self.assertFalse(row["has_rejected_update"])

    def test_api_approved_update_does_not_mark_asset_as_rejected(self):
        requester = User.objects.create_user(username="approved-rejected-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Approved Update Asset",
            inventory_number="APPROVED-UPD-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.APPROVED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Approved Update Asset Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertFalse(row["has_rejected_update"])

    def test_api_marks_pending_and_rejected_update_flags_independently(self):
        requester = User.objects.create_user(username="pending-rejected-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Pending And Rejected Update Asset",
            inventory_number="PENDING-REJECTED-UPD-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.REJECTED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Rejected Name"}},
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Pending Name"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_pending_update"])
        self.assertTrue(row["has_rejected_update"])

    def test_api_pending_update_marker_does_not_add_per_row_queries(self):
        requester = User.objects.create_user(username="pending-query-requester", password="test-pass-123")
        assets = [
            Asset.objects.create(
                name=f"Pending Query Asset {index}",
                inventory_number=f"PENDING-QUERY-{index:03d}",
                status=Asset.Status.IN_STOCK,
                location="Pending Query",
                category="IT",
            )
            for index in range(3)
        ]
        for asset in assets:
            AssetChangeRequest.objects.create(
                requested_by=requester,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.PENDING,
                asset=asset,
                payload={"current": {"name": asset.name}, "proposed": {"name": f"{asset.name} Edited"}},
            )
            AssetChangeRequest.objects.create(
                requested_by=requester,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.REJECTED,
                asset=asset,
                payload={"current": {"name": asset.name}, "proposed": {"name": f"{asset.name} Rejected"}},
            )

        with self.assertNumQueries(4):
            response = self.client.get(
                reverse("assets:api-list"),
                {"search": "PENDING-QUERY", "page_size": 3},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 3)


class AssetBulkMoveApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="bulk-admin",
            email="bulk-admin@example.com",
            password="test-pass-123",
        )
        cls.root_location = Location.objects.create(name="Warszawa")
        cls.target_location = Location.objects.create(name="Budynek A", parent=cls.root_location)
        cls.inactive_location = Location.objects.create(name="Archiwum", parent=cls.root_location, is_active=False)
        cls.asset_one = Asset.objects.create(
            name="Bulk Asset 1",
            inventory_number="BULK-001",
            status=Asset.Status.IN_STOCK,
            location="HQ",
            location_fk=cls.root_location,
            category="IT",
        )
        cls.asset_two = Asset.objects.create(
            name="Bulk Asset 2",
            inventory_number="BULK-002",
            status=Asset.Status.IN_USE,
            location="Branch",
            location_fk=cls.root_location,
            category="IT",
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_bulk_move_updates_assets(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_one.id, self.asset_two.id],
                "target_location_id": self.target_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "success": True,
                "updated_count": 2,
                "target_location_id": self.target_location.id,
                "target_location_path": self.target_location.path,
            },
        )
        self.asset_one.refresh_from_db()
        self.asset_two.refresh_from_db()
        self.assertEqual(self.asset_one.location, self.target_location.path)
        self.assertEqual(self.asset_two.location, self.target_location.path)
        self.assertEqual(self.asset_one.location_fk, self.target_location)
        self.assertEqual(self.asset_two.location_fk, self.target_location)

    def test_bulk_move_rejects_empty_asset_ids(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [], "target_location_id": self.target_location.id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["success"], False)

    def test_bulk_move_rejects_invalid_target_location_id(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [self.asset_one.id], "target_location_id": ""},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["success"], False)

    def test_bulk_move_rejects_missing_or_inactive_target_location_id(self):
        missing_response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [self.asset_one.id]},
            content_type="application/json",
        )
        inactive_response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [self.asset_one.id], "target_location_id": self.inactive_location.id},
            content_type="application/json",
        )

        self.assertEqual(missing_response.status_code, 400)
        self.assertEqual(missing_response.json()["success"], False)
        self.assertEqual(inactive_response.status_code, 400)
        self.assertEqual(inactive_response.json()["success"], False)

    def test_bulk_move_rejects_get(self):
        response = self.client.get(reverse("assets:api-bulk-move"))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["success"], False)


class AssetBulkMoveApiAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root_location = Location.objects.create(name="Warszawa")
        cls.child_location = Location.objects.create(name="Budynek A", parent=cls.root_location)
        cls.grandchild_location = Location.objects.create(name="Pietro 1", parent=cls.child_location)
        cls.other_root_location = Location.objects.create(name="Krakow")
        cls.other_child_location = Location.objects.create(name="Magazyn", parent=cls.other_root_location)

        cls.admin_user = User.objects.create_superuser(
            username="bulk-scope-admin",
            email="bulk-scope-admin@example.com",
            password="test-pass-123",
        )
        cls.manager_user = User.objects.create_user(username="bulk-scope-manager", password="test-pass-123")
        cls.manager_user.profile.role = UserProfile.Role.MANAGER
        cls.manager_user.profile.save(update_fields=["role"])
        cls.manager_user.profile.allowed_locations.add(cls.root_location)

        cls.asset_in_scope = Asset.objects.create(
            name="In Scope",
            inventory_number="BULK-S-001",
            status=Asset.Status.IN_STOCK,
            location=cls.child_location.path,
            location_fk=cls.child_location,
            category="IT",
        )
        cls.asset_in_scope_two = Asset.objects.create(
            name="In Scope Two",
            inventory_number="BULK-S-002",
            status=Asset.Status.IN_STOCK,
            location=cls.grandchild_location.path,
            location_fk=cls.grandchild_location,
            category="IT",
        )
        cls.asset_out_of_scope = Asset.objects.create(
            name="Out of Scope",
            inventory_number="BULK-S-003",
            status=Asset.Status.IN_STOCK,
            location=cls.other_child_location.path,
            location_fk=cls.other_child_location,
            category="IT",
        )
        cls.asset_without_fk = Asset.objects.create(
            name="Without FK",
            inventory_number="BULK-S-004",
            status=Asset.Status.IN_STOCK,
            location="Legacy only",
            location_fk=None,
            category="IT",
        )

    def test_admin_can_bulk_move_any_asset_to_any_location(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_out_of_scope.id],
                "target_location_id": self.child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.asset_out_of_scope.refresh_from_db()
        self.assertEqual(self.asset_out_of_scope.location, self.child_location.path)
        self.assertEqual(self.asset_out_of_scope.location_fk, self.child_location)

    def test_user_can_move_asset_within_allowed_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_in_scope.id],
                "target_location_id": self.grandchild_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.asset_in_scope.refresh_from_db()
        self.assertEqual(self.asset_in_scope.location, self.grandchild_location.path)
        self.assertEqual(self.asset_in_scope.location_fk, self.grandchild_location)

    def test_user_cannot_move_asset_outside_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_out_of_scope.id],
                "target_location_id": self.child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_out_of_scope.refresh_from_db()
        self.assertEqual(self.asset_out_of_scope.location_fk, self.other_child_location)

    def test_user_cannot_move_asset_to_location_outside_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_in_scope.id],
                "target_location_id": self.other_child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_in_scope.refresh_from_db()
        self.assertEqual(self.asset_in_scope.location_fk, self.child_location)

    def test_user_cannot_move_asset_with_null_location_fk(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_without_fk.id],
                "target_location_id": self.child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_without_fk.refresh_from_db()
        self.assertIsNone(self.asset_without_fk.location_fk)
        self.assertEqual(self.asset_without_fk.location, "Legacy only")

    def test_unauthorized_request_does_not_partially_move_assets(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_in_scope.id, self.asset_out_of_scope.id],
                "target_location_id": self.grandchild_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_in_scope.refresh_from_db()
        self.asset_out_of_scope.refresh_from_db()
        self.assertEqual(self.asset_in_scope.location, self.child_location.path)
        self.assertEqual(self.asset_in_scope.location_fk, self.child_location)
        self.assertEqual(self.asset_out_of_scope.location, self.other_child_location.path)
        self.assertEqual(self.asset_out_of_scope.location_fk, self.other_child_location)


class AssetListApiLocationAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root_location = Location.objects.create(name="Warszawa")
        cls.child_location = Location.objects.create(name="Budynek A", parent=cls.root_location)
        cls.grandchild_location = Location.objects.create(name="Pietro 1", parent=cls.child_location)
        cls.other_root_location = Location.objects.create(name="Krakow")
        cls.other_child_location = Location.objects.create(name="Magazyn", parent=cls.other_root_location)

        cls.asset_root = Asset.objects.create(
            name="Asset Root",
            inventory_number="ACL-001",
            status=Asset.Status.IN_STOCK,
            location="nieuzywane-root",
            location_fk=cls.root_location,
            category="IT",
        )
        cls.asset_child = Asset.objects.create(
            name="Asset Child",
            inventory_number="ACL-002",
            status=Asset.Status.IN_STOCK,
            location="nieuzywane-child",
            location_fk=cls.child_location,
            category="IT",
        )
        cls.asset_grandchild = Asset.objects.create(
            name="Asset Grandchild",
            inventory_number="ACL-003",
            status=Asset.Status.IN_STOCK,
            location="nieuzywane-grandchild",
            location_fk=cls.grandchild_location,
            category="IT",
        )
        cls.asset_outside = Asset.objects.create(
            name="Asset Outside",
            inventory_number="ACL-004",
            status=Asset.Status.IN_STOCK,
            location="nieuzywane-outside",
            location_fk=cls.other_child_location,
            category="IT",
        )
        cls.asset_without_fk = Asset.objects.create(
            name="Asset Without FK",
            inventory_number="ACL-005",
            status=Asset.Status.IN_STOCK,
            location="nieuzywane-null",
            location_fk=None,
            category="IT",
        )

        cls.admin_user = User.objects.create_superuser(
            username="scope-admin",
            email="scope-admin@example.com",
            password="test-pass-123",
        )
        cls.manager_user = User.objects.create_user(username="scope-manager", password="test-pass-123")
        cls.manager_user.profile.role = UserProfile.Role.MANAGER
        cls.manager_user.profile.save(update_fields=["role"])
        cls.manager_user.profile.allowed_locations.add(cls.root_location)

        cls.no_access_user = User.objects.create_user(username="scope-empty", password="test-pass-123")
        cls.no_access_user.profile.role = UserProfile.Role.USER
        cls.no_access_user.profile.save(update_fields=["role"])

    def test_admin_sees_all_assets(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        returned_ids = {row["id"] for row in payload["results"]}
        self.assertEqual(payload["pagination"]["total_items"], 5)
        self.assertSetEqual(
            returned_ids,
            {
                self.asset_root.id,
                self.asset_child.id,
                self.asset_grandchild.id,
                self.asset_outside.id,
                self.asset_without_fk.id,
            },
        )

    def test_user_sees_asset_from_allowed_location(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"), {"search": "ACL-001"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["id"], self.asset_root.id)

    def test_user_sees_asset_from_descendant_location(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"), {"search": "ACL-003"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["id"], self.asset_grandchild.id)

    def test_user_does_not_see_assets_outside_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        returned_ids = {row["id"] for row in payload["results"]}
        self.assertEqual(payload["pagination"]["total_items"], 3)
        self.assertIn(self.asset_root.id, returned_ids)
        self.assertIn(self.asset_child.id, returned_ids)
        self.assertIn(self.asset_grandchild.id, returned_ids)
        self.assertNotIn(self.asset_outside.id, returned_ids)

    def test_user_without_allowed_locations_sees_no_assets(self):
        self.client.force_login(self.no_access_user)

        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 0)
        self.assertEqual(payload["results"], [])

    def test_user_does_not_see_assets_with_null_location_fk(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"), {"search": "ACL-005"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 0)
        self.assertEqual(payload["results"], [])


class AssetListViewTests(TestCase):
    def test_list_view_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_list_view_exposes_filters_for_authenticated_user(self):
        Asset.objects.create(
            name="Monitor",
            inventory_number="MON-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_user(username="viewer", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-api-url="/api/assets/"')
        self.assertContains(response, "<option value=\"Warehouse\">Warehouse</option>", html=True)

    def test_list_view_contains_pending_update_marker_renderer(self):
        user = User.objects.create_user(username="viewer-pending-marker", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'pending.textContent = " • Oczekuje";')

    def test_list_view_contains_rejected_update_marker_renderer_with_pending_priority(self):
        user = User.objects.create_user(username="viewer-rejected-marker", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'if (row.has_pending_update) {')
        self.assertContains(response, 'pending.textContent = " • Oczekuje";')
        self.assertContains(response, '} else if (row.has_rejected_update) {')
        self.assertContains(response, 'rejected.textContent = " • Odrzucono";')


class AssetDetailViewTests(TestCase):
    def test_detail_view_redirects_anonymous_user_to_login(self):
        asset = Asset.objects.create(
            name="Detail Anonymous",
            inventory_number="DETAIL-ANON-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_authenticated_user_can_view_asset_detail(self):
        location = Location.objects.create(name="Warehouse")
        asset = Asset.objects.create(
            name="Detail Laptop",
            inventory_number="DETAIL-001",
            status=Asset.Status.IN_STOCK,
            location=location.path,
            location_fk=location,
            category="IT",
        )
        user = User.objects.create_user(username="detail-viewer", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Detail Laptop")
        self.assertContains(response, "DETAIL-001")

    def test_detail_view_returns_404_for_asset_outside_user_scope(self):
        root_location = Location.objects.create(name="Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        asset = Asset.objects.create(
            name="Out Of Scope Detail",
            inventory_number="DETAIL-SCOPE-001",
            status=Asset.Status.IN_STOCK,
            location=outside_location.path,
            location_fk=outside_location,
            category="IT",
        )
        user = User.objects.create_user(username="detail-scoped-user", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 404)

    def test_detail_view_returns_404_for_user_asset_without_location_fk(self):
        root_location = Location.objects.create(name="Poznan")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        asset = Asset.objects.create(
            name="No FK Detail",
            inventory_number="DETAIL-NO-FK-001",
            status=Asset.Status.IN_STOCK,
            location="Legacy only",
            location_fk=None,
            category="IT",
        )
        user = User.objects.create_user(username="detail-no-fk-user", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 404)

    def test_admin_role_and_superuser_can_view_out_of_scope_and_null_location_assets(self):
        outside_location = Location.objects.create(name="Outside")
        out_of_scope_asset = Asset.objects.create(
            name="Admin Out Of Scope Detail",
            inventory_number="DETAIL-ADMIN-SCOPE-001",
            status=Asset.Status.IN_STOCK,
            location=outside_location.path,
            location_fk=outside_location,
            category="IT",
        )
        null_location_asset = Asset.objects.create(
            name="Admin Null FK Detail",
            inventory_number="DETAIL-ADMIN-NO-FK-001",
            status=Asset.Status.IN_STOCK,
            location="Legacy only",
            location_fk=None,
            category="IT",
        )
        admin_user = User.objects.create_user(username="detail-admin-role", password="test-pass-123")
        admin_user.profile.role = UserProfile.Role.ADMIN
        admin_user.profile.save(update_fields=["role"])
        superuser = User.objects.create_superuser(
            username="detail-superuser",
            email="detail-superuser@example.com",
            password="test-pass-123",
        )

        for user in (admin_user, superuser):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                out_of_scope_response = self.client.get(reverse("assets:detail", kwargs={"id": out_of_scope_asset.id}))
                null_location_response = self.client.get(reverse("assets:detail", kwargs={"id": null_location_asset.id}))
                self.client.logout()

                self.assertEqual(out_of_scope_response.status_code, 200)
                self.assertEqual(null_location_response.status_code, 200)

    def test_detail_view_links_to_public_update_url(self):
        asset = Asset.objects.create(
            name="Public Update Link",
            inventory_number="DETAIL-UPDATE-LINK-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_superuser(
            username="detail-update-link",
            email="detail-update-link@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("assets:update", kwargs={"pk": asset.id}))


class AssetUpdateViewTests(TestCase):
    def _messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def _create_location_tree(self):
        root_location = Location.objects.create(name="Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number="UPDATE-001", location_obj=None, **overrides):
        location_value = location_obj.path if location_obj else "Warehouse"
        defaults = {
            "name": "Update Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED_ASSET,
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "location": location_value,
            "location_fk": location_obj,
            "category": "IT",
            "is_active": True,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _valid_update_payload(self, asset, **overrides):
        payload = {
            "name": "Updated Asset",
            "inventory_number": asset.inventory_number,
            "asset_type": Asset.AssetType.IT_EQUIPMENT,
            "category": "Updated IT",
            "manufacturer": "Dell",
            "model": "Latitude",
            "serial_number": "SN-UPDATED",
            "barcode": "",
            "description": "Updated description",
            "purchase_date": "",
            "commissioning_date": "",
            "purchase_value": "",
            "invoice_number": "",
            "external_id": "",
            "cost_center": "",
            "organizational_unit": "",
            "department": "",
            "location": asset.location,
            "room": "101",
            "responsible_person": "",
            "current_user": "",
            "status": Asset.Status.IN_USE,
            "technical_condition": Asset.TechnicalCondition.VERY_GOOD,
            "last_inventory_date": "",
            "next_review_date": "",
            "warranty_until": "",
            "insurance_until": "",
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    def _manager_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        return user

    def test_update_view_redirects_anonymous_user_to_login(self):
        location = Location.objects.create(name="Warehouse")
        asset = self._create_asset(location_obj=location)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_user_in_scope_can_open_update_form(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-in-scope", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, asset.name)

    def test_user_outside_scope_gets_404_for_update_form(self):
        allowed_location, outside_location = self._create_location_tree()
        asset = self._create_asset(location_obj=outside_location)
        user = self._manager_with_location("update-outside-scope", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 404)

    def test_user_requiring_approval_cannot_queue_update_outside_scope(self):
        allowed_location, outside_location = self._create_location_tree()
        asset = self._create_asset(location_obj=outside_location)
        user = self._manager_with_location("update-approval-outside-scope", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Should Not Queue"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(asset.name, "Update Asset")
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_user_cannot_update_asset_without_location_fk(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=None, location_fk=None, location="Legacy only")
        user = self._manager_with_location("update-no-fk", allowed_location)
        self.client.force_login(user)

        get_response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))
        post_response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Should Not Save"),
        )

        asset.refresh_from_db()
        self.assertEqual(get_response.status_code, 404)
        self.assertEqual(post_response.status_code, 404)
        self.assertEqual(asset.name, "Update Asset")

    def test_user_requiring_approval_cannot_queue_update_for_asset_without_location_fk(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=None, location_fk=None, location="Legacy only")
        user = self._manager_with_location("update-approval-no-fk", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Should Not Queue"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(asset.name, "Update Asset")
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_admin_role_can_open_out_of_scope_update_form(self):
        _, outside_location = self._create_location_tree()
        asset = self._create_asset(location_obj=outside_location)
        user = User.objects.create_user(username="update-admin-role", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.save(update_fields=["role"])
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)

    def test_superuser_can_open_out_of_scope_and_null_location_update_forms(self):
        _, outside_location = self._create_location_tree()
        out_of_scope_asset = self._create_asset(inventory_number="UPDATE-SUPER-001", location_obj=outside_location)
        null_location_asset = self._create_asset(
            inventory_number="UPDATE-SUPER-002",
            location_obj=None,
            location_fk=None,
            location="Legacy only",
        )
        user = User.objects.create_superuser(
            username="update-superuser",
            email="update-superuser@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        out_of_scope_response = self.client.get(reverse("assets:update", kwargs={"pk": out_of_scope_asset.pk}))
        null_location_response = self.client.get(reverse("assets:update", kwargs={"pk": null_location_asset.pk}))

        self.assertEqual(out_of_scope_response.status_code, 200)
        self.assertEqual(null_location_response.status_code, 200)

    def test_valid_update_post_saves_changes_and_redirects_to_detail(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-post", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Saved Update", location="Updated legacy location"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("assets:detail", kwargs={"id": asset.pk}))
        self.assertEqual(asset.name, "Saved Update")
        self.assertEqual(asset.location, "Updated legacy location")
        self.assertEqual(asset.status, Asset.Status.IN_USE)
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_user_requiring_approval_queues_update_without_saving_asset(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Original Name", location=allowed_location.path)
        user = self._manager_with_location("update-approval-required", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(
                asset,
                name="Queued Update",
                location="Queued legacy location",
                malicious_field="not-in-payload",
            ),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("assets:detail", kwargs={"id": asset.pk}))
        self.assertEqual(asset.name, "Original Name")
        self.assertEqual(asset.location, allowed_location.path)
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        change_request = AssetChangeRequest.objects.get()
        self.assertEqual(change_request.operation, AssetChangeRequest.Operation.UPDATE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.asset, asset)
        self.assertEqual(change_request.requested_by, user)
        self.assertSetEqual(set(change_request.payload.keys()), {"current", "proposed"})
        self.assertEqual(change_request.payload["current"]["name"], "Original Name")
        self.assertEqual(change_request.payload["current"]["location"], allowed_location.path)
        self.assertEqual(change_request.payload["proposed"]["name"], "Queued Update")
        self.assertEqual(change_request.payload["proposed"]["location"], "Queued legacy location")
        self.assertNotIn("malicious_field", change_request.payload["current"])
        self.assertNotIn("malicious_field", change_request.payload["proposed"])
        self.assertEqual(self._messages(response), ["Zmiana została przekazana do akceptacji."])

    def test_user_requiring_approval_updates_existing_pending_update_request(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Original Name", location=allowed_location.path)
        first_user = self._manager_with_location("update-pending-first", allowed_location)
        first_user.profile.asset_changes_require_approval = True
        first_user.profile.save(update_fields=["asset_changes_require_approval"])
        second_user = self._manager_with_location("update-pending-second", allowed_location)
        second_user.profile.asset_changes_require_approval = True
        second_user.profile.save(update_fields=["asset_changes_require_approval"])

        self.client.force_login(first_user)
        first_response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="First Queued Update", room="101"),
        )
        self.assertEqual(self._messages(first_response), ["Zmiana została przekazana do akceptacji."])
        first_request = AssetChangeRequest.objects.get()

        second_client = Client()
        second_client.force_login(second_user)
        second_response = second_client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Second Queued Update", room="202"),
        )

        asset.refresh_from_db()
        first_request.refresh_from_db()
        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        self.assertEqual(first_request.operation, AssetChangeRequest.Operation.UPDATE)
        self.assertEqual(first_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(first_request.asset, asset)
        self.assertEqual(first_request.requested_by, first_user)
        self.assertEqual(first_request.payload["current"]["name"], "Original Name")
        self.assertEqual(first_request.payload["proposed"]["name"], "Second Queued Update")
        self.assertEqual(first_request.payload["proposed"]["room"], "202")
        self.assertEqual(asset.name, "Original Name")
        self.assertEqual(asset.room, "")
        self.assertEqual(self._messages(second_response), ["Oczekująca zmiana została zaktualizowana."])

    def test_user_requiring_approval_creates_separate_pending_update_for_other_asset(self):
        allowed_location, _ = self._create_location_tree()
        first_asset = self._create_asset(
            inventory_number="UPDATE-PENDING-OTHER-001",
            location_obj=allowed_location,
            name="First Original",
        )
        second_asset = self._create_asset(
            inventory_number="UPDATE-PENDING-OTHER-002",
            location_obj=allowed_location,
            name="Second Original",
        )
        user = self._manager_with_location("update-pending-other-asset", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        self.client.post(
            reverse("assets:update", kwargs={"pk": first_asset.pk}),
            data=self._valid_update_payload(first_asset, name="First Queued"),
        )
        response = self.client.post(
            reverse("assets:update", kwargs={"pk": second_asset.pk}),
            data=self._valid_update_payload(second_asset, name="Second Queued"),
        )

        first_asset.refresh_from_db()
        second_asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AssetChangeRequest.objects.count(), 2)
        self.assertTrue(
            AssetChangeRequest.objects.filter(
                asset=first_asset,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.PENDING,
            ).exists()
        )
        self.assertTrue(
            AssetChangeRequest.objects.filter(
                asset=second_asset,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.PENDING,
            ).exists()
        )
        self.assertEqual(first_asset.name, "First Original")
        self.assertEqual(second_asset.name, "Second Original")

    def test_invalid_update_post_does_not_save_changes(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-invalid", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="", inventory_number=""),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(asset.name, "Update Asset")
        self.assertEqual(asset.inventory_number, "UPDATE-001")
        self.assertFalse(AssetChangeRequest.objects.exists())
        self.assertIn("name", response.context["form"].errors)
        self.assertIn("inventory_number", response.context["form"].errors)

    def test_update_ignores_extra_post_fields(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-extra-field", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, malicious_field="ignored"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(hasattr(asset, "malicious_field"))

    def test_update_saves_legacy_location_string_as_text(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-legacy-location", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, location="LOKALIZACJA SPOZA ZAKRESU"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.location, "LOKALIZACJA SPOZA ZAKRESU")
        self.assertEqual(asset.location_fk, allowed_location)

    def test_superuser_and_approver_update_asset_without_queue(self):
        allowed_location, _ = self._create_location_tree()
        users = []

        superuser = User.objects.create_superuser(
            username="update-bypass-superuser",
            email="update-bypass-superuser@example.com",
            password="test-pass-123",
        )
        superuser.profile.asset_changes_require_approval = True
        superuser.profile.save(update_fields=["asset_changes_require_approval"])
        users.append(("superuser", superuser))

        approver = User.objects.create_user(username="update-bypass-approver", password="test-pass-123")
        approver.profile.can_approve_asset_changes = True
        approver.profile.asset_changes_require_approval = True
        approver.profile.save(update_fields=["can_approve_asset_changes", "asset_changes_require_approval"])
        approver.profile.allowed_locations.add(allowed_location)
        users.append(("approver", approver))

        for index, (label, user) in enumerate(users, start=1):
            asset = self._create_asset(
                inventory_number=f"UPDATE-BYPASS-{index:03d}",
                location_obj=allowed_location,
                name=f"Bypass Original {index}",
            )
            with self.subTest(label=label):
                self.client.force_login(user)
                response = self.client.post(
                    reverse("assets:update", kwargs={"pk": asset.pk}),
                    data=self._valid_update_payload(asset, name=f"Bypass Saved {index}"),
                )
                self.client.logout()

                asset.refresh_from_db()
                self.assertEqual(response.status_code, 302)
                self.assertEqual(asset.name, f"Bypass Saved {index}")

        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_admin_role_with_approval_required_queues_update_without_saving_asset(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Admin Original")
        user = User.objects.create_user(username="update-admin-approval-required", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Admin Queued Update"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.name, "Admin Original")
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        change_request = AssetChangeRequest.objects.get()
        self.assertEqual(change_request.operation, AssetChangeRequest.Operation.UPDATE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.requested_by, user)

    def test_admin_role_without_approval_required_updates_asset_directly(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Admin Direct Original")
        user = User.objects.create_user(username="update-admin-direct", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = False
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Admin Direct Saved"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.name, "Admin Direct Saved")
        self.assertFalse(AssetChangeRequest.objects.exists())


class AssetCreateViewTests(TestCase):
    def _valid_asset_payload(self, **overrides):
        payload = {
            "name": "Created Asset",
            "inventory_number": "CREATE-001",
            "asset_type": Asset.AssetType.FIXED_ASSET,
            "status": Asset.Status.IN_STOCK,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "location": "Warehouse",
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    def test_create_view_redirects_anonymous_user_and_does_not_create_asset(self):
        response = self.client.post(reverse("assets:create"), data=self._valid_asset_payload())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))
        self.assertFalse(Asset.objects.filter(inventory_number="CREATE-001").exists())

    def test_authenticated_user_can_create_asset(self):
        user = User.objects.create_user(username="asset-creator", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.post(reverse("assets:create"), data=self._valid_asset_payload())

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AssetChangeRequest.objects.exists())
        self.assertEqual(Asset.objects.filter(inventory_number="CREATE-001").count(), 1)
        asset = Asset.objects.get(inventory_number="CREATE-001")
        self.assertEqual(asset.name, "Created Asset")
        self.assertEqual(asset.asset_type, Asset.AssetType.FIXED_ASSET)
        self.assertEqual(asset.status, Asset.Status.IN_STOCK)
        self.assertEqual(asset.technical_condition, Asset.TechnicalCondition.GOOD)
        self.assertEqual(asset.location, "Warehouse")

    def test_invalid_create_form_does_not_create_asset(self):
        user = User.objects.create_user(username="asset-invalid", password="test-pass-123")
        self.client.force_login(user)
        asset_count = Asset.objects.count()

        response = self.client.post(reverse("assets:create"), data={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Asset.objects.count(), asset_count)
        self.assertFalse(AssetChangeRequest.objects.exists())
        self.assertIn("name", response.context["form"].errors)
        self.assertIn("inventory_number", response.context["form"].errors)

    def test_user_requiring_approval_creates_change_request_without_asset(self):
        user = User.objects.create_user(username="asset-approval-required", password="test-pass-123")
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-APPROVAL-001",
                name="Queued Asset",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Asset.objects.filter(inventory_number="CREATE-APPROVAL-001").exists())
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        change_request = AssetChangeRequest.objects.get()
        self.assertEqual(change_request.operation, AssetChangeRequest.Operation.CREATE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertEqual(change_request.requested_by, user)
        self.assertEqual(change_request.payload["name"], "Queued Asset")
        self.assertEqual(change_request.payload["inventory_number"], "CREATE-APPROVAL-001")
        self.assertEqual(change_request.payload["asset_type"], Asset.AssetType.FIXED_ASSET)
        self.assertEqual(change_request.payload["status"], Asset.Status.IN_STOCK)
        self.assertEqual(change_request.payload["technical_condition"], Asset.TechnicalCondition.GOOD)
        self.assertEqual(change_request.payload["location"], "Warehouse")

    def test_approval_payload_uses_cleaned_data_and_ignores_extra_post_fields(self):
        user = User.objects.create_user(username="asset-approval-payload", password="test-pass-123")
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-PAYLOAD-001",
                malicious_field="should-not-be-saved",
            ),
        )

        self.assertEqual(response.status_code, 302)
        change_request = AssetChangeRequest.objects.get()
        self.assertNotIn("malicious_field", change_request.payload)

    def test_superuser_and_approver_create_asset_without_queue(self):
        users = []

        superuser = User.objects.create_superuser(
            username="asset-create-superuser",
            email="asset-create-superuser@example.com",
            password="test-pass-123",
        )
        superuser.profile.asset_changes_require_approval = True
        superuser.profile.save(update_fields=["asset_changes_require_approval"])
        users.append(("superuser", superuser))

        approver = User.objects.create_user(username="asset-create-approver", password="test-pass-123")
        approver.profile.can_approve_asset_changes = True
        approver.profile.asset_changes_require_approval = True
        approver.profile.save(update_fields=["can_approve_asset_changes", "asset_changes_require_approval"])
        users.append(("approver", approver))

        for index, (label, user) in enumerate(users, start=1):
            with self.subTest(label=label):
                self.client.force_login(user)
                response = self.client.post(
                    reverse("assets:create"),
                    data=self._valid_asset_payload(inventory_number=f"CREATE-BYPASS-{index:03d}"),
                )
                self.client.logout()

                self.assertEqual(response.status_code, 302)
                self.assertTrue(Asset.objects.filter(inventory_number=f"CREATE-BYPASS-{index:03d}").exists())

        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_admin_role_with_approval_required_queues_create_without_saving_asset(self):
        user = User.objects.create_user(username="asset-create-admin-approval-required", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-ADMIN-APPROVAL-001",
                name="Admin Queued Asset",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Asset.objects.filter(inventory_number="CREATE-ADMIN-APPROVAL-001").exists())
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        change_request = AssetChangeRequest.objects.get()
        self.assertEqual(change_request.operation, AssetChangeRequest.Operation.CREATE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.requested_by, user)
        self.assertEqual(change_request.payload["name"], "Admin Queued Asset")

    def test_admin_role_without_approval_required_creates_asset_directly(self):
        user = User.objects.create_user(username="asset-create-admin-direct", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = False
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-ADMIN-DIRECT-001",
                name="Admin Direct Asset",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Asset.objects.filter(inventory_number="CREATE-ADMIN-DIRECT-001").exists())
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_create_ignores_posted_location_fk(self):
        user = User.objects.create_user(username="asset-location-fk", password="test-pass-123")
        location = Location.objects.create(name="Ignored Location")
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-FK-001",
                location_fk=str(location.id),
            ),
        )

        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get(inventory_number="CREATE-FK-001")
        self.assertIsNone(asset.location_fk)

    def test_create_accepts_legacy_location_string_without_scope_validation(self):
        user = User.objects.create_user(username="asset-legacy-location", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-LOC-001",
                location="LOKALIZACJA SPOZA ZAKRESU",
            ),
        )

        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get(inventory_number="CREATE-LOC-001")
        self.assertEqual(asset.location, "LOKALIZACJA SPOZA ZAKRESU")


class SeedAssetsCommandTests(TestCase):
    def test_seed_assets_creates_requested_records(self):
        stdout = StringIO()

        call_command("seed_assets", "--count", "25", stdout=stdout)

        seeded_assets = Asset.objects.filter(external_id__startswith="seed_asset:")
        self.assertEqual(seeded_assets.count(), 25)
        self.assertTrue(all(asset.inventory_number.startswith("SEED-AST-") for asset in seeded_assets))
        self.assertIn("Utworzono: 25", stdout.getvalue())

    def test_seed_assets_clear_removes_only_seeded_records(self):
        Asset.objects.create(
            name="Existing Asset",
            inventory_number="INV-EXIST-001",
            status=Asset.Status.IN_STOCK,
            location="HQ",
            category="IT",
        )
        call_command("seed_assets", "--count", "10")

        stdout = StringIO()
        call_command("seed_assets", "--count", "0", "--clear", stdout=stdout)

        self.assertTrue(Asset.objects.filter(inventory_number="INV-EXIST-001").exists())
        self.assertFalse(Asset.objects.filter(external_id__startswith="seed_asset:").exists())
        self.assertIn("Usunieto 10", stdout.getvalue())


class BackfillAssetLocationFkCommandTests(TestCase):
    def test_dry_run_reports_matches_without_saving(self):
        root = Location.objects.create(name="Warszawa")
        target = Location.objects.create(name="Magazyn A", parent=root)
        matching_asset = Asset.objects.create(
            name="Laptop Match",
            inventory_number="BF-001",
            status=Asset.Status.IN_STOCK,
            location=target.path,
            category="IT",
        )
        Asset.objects.create(
            name="Laptop Empty",
            inventory_number="BF-002",
            status=Asset.Status.IN_STOCK,
            location="",
            category="IT",
        )
        Asset.objects.create(
            name="Laptop Miss",
            inventory_number="BF-003",
            status=Asset.Status.IN_STOCK,
            location="Nieistniejaca / Sciezka",
            category="IT",
        )

        stdout = StringIO()
        call_command("backfill_asset_location_fk", "--dry-run", stdout=stdout)

        matching_asset.refresh_from_db()
        self.assertIsNone(matching_asset.location_fk)
        self.assertIn("Pewne dopasowania: 1", stdout.getvalue())
        self.assertIn("Puste location: 1", stdout.getvalue())
        self.assertIn("Bez dopasowania: 1", stdout.getvalue())
        self.assertIn("Pozostaje bez location_fk: 2", stdout.getvalue())

    def test_command_backfills_only_exact_path_matches(self):
        root = Location.objects.create(name="Krakow")
        target = Location.objects.create(name="Biuro", parent=root)
        matching_asset = Asset.objects.create(
            name="Laptop Match 2",
            inventory_number="BF-010",
            status=Asset.Status.IN_STOCK,
            location=target.path,
            category="IT",
        )
        unmatched_asset = Asset.objects.create(
            name="Laptop Miss 2",
            inventory_number="BF-011",
            status=Asset.Status.IN_STOCK,
            location="Krakow / Nieistniejace",
            category="IT",
        )

        stdout = StringIO()
        call_command("backfill_asset_location_fk", stdout=stdout)

        matching_asset.refresh_from_db()
        unmatched_asset.refresh_from_db()
        self.assertEqual(matching_asset.location_fk_id, target.id)
        self.assertIsNone(unmatched_asset.location_fk)
        self.assertIn("Pewne dopasowania: 1", stdout.getvalue())
        self.assertIn("Bez dopasowania: 1", stdout.getvalue())
        self.assertIn("Pozostaje bez location_fk: 1", stdout.getvalue())
