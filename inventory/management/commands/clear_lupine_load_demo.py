from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from assets.models import Asset, AssetAttachment, AssetChangeRequest, AssetHistoryEntry, AssetServiceAlert
from inventory.models import InventorySession
from locations.models import Location

LOAD_DEMO_PREFIX = "load_demo:"
LOAD_SESSION_PREFIX = "LOAD-"
LOAD_USER_PREFIX = "load_demo_"
LOAD_ROOT_NAME = "LOAD Demo"


class Command(BaseCommand):
    help = "Usuwa dane load-demo z bazy danych (markery: load_demo:, LOAD-, load_demo_)."

    def handle(self, *args, **options):
        self.stdout.write("[clear-load-demo] Uruchamianie czyszczenia...")

        # 1. Sesje inventory → CASCADE usuwa snapshoty, observed items, batche
        deleted = InventorySession.objects.filter(number__startswith=LOAD_SESSION_PREFIX).delete()
        n_sessions = deleted[1].get("inventory.InventorySession", 0)
        self.stdout.write(f"  Sesje inventory:      {n_sessions}")

        # 2. IDs assetów load-demo – potrzebne do kolejnych kroków
        load_asset_ids = list(
            Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).values_list("id", flat=True)
        )

        if load_asset_ids:
            # 3. Załączniki – usunąć pliki z dysku przed usunięciem rekordów
            attachments_qs = AssetAttachment.objects.filter(asset_id__in=load_asset_ids)
            att_count = 0
            for att in attachments_qs.iterator():
                try:
                    att.file.delete(save=False)
                except Exception:
                    pass
                att_count += 1
            if att_count:
                attachments_qs.delete()
            self.stdout.write(f"  Załączniki:           {att_count}")

            # 4. Zlicz powiązane rekordy przed usunięciem assetów (CASCADE)
            n_alerts = AssetServiceAlert.objects.filter(asset_id__in=load_asset_ids).count()
            n_history = AssetHistoryEntry.objects.filter(asset_id__in=load_asset_ids).count()
            n_changes = AssetChangeRequest.objects.filter(asset_id__in=load_asset_ids).count()

            # 5. Usuń assety → CASCADE usuwa alerty, historię, wnioski, snapshotem asset SET_NULL
            asset_del = Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).delete()
            n_assets = asset_del[1].get("assets.Asset", 0)
            self.stdout.write(f"  Assety:               {n_assets}")
            self.stdout.write(f"    ↳ alerty:           {n_alerts}")
            self.stdout.write(f"    ↳ historia:         {n_history}")
            self.stdout.write(f"    ↳ wnioski zmian:    {n_changes}")
        else:
            self.stdout.write("  Assety:               0 (brak danych load-demo)")

        # 6. Użytkownicy → CASCADE usuwa profil
        User = get_user_model()
        user_del = User.objects.filter(username__startswith=LOAD_USER_PREFIX).delete()
        n_users = user_del[1].get("users.User", 0)
        self.stdout.write(f"  Użytkownicy:          {n_users}")

        # 7. Lokalizacje – od liści do korzenia (FK PROTECT wymaga kolejności)
        n_locations = self._delete_load_locations()
        self.stdout.write(f"  Lokalizacje:          {n_locations}")

        self.stdout.write(self.style.SUCCESS("[clear-load-demo] Czyszczenie zakończone."))

    def _delete_load_locations(self):
        try:
            root = Location.objects.get(name=LOAD_ROOT_NAME, parent__isnull=True)
        except Location.DoesNotExist:
            return 0

        all_ids = self._collect_subtree_ids(root.id)
        total = 0

        # Kasuj porcjami od liści (max 20 przebiegów odpowiada głębokości drzewa)
        for _ in range(20):
            if not all_ids:
                break
            leaf_ids = list(
                Location.objects.filter(id__in=all_ids, children__isnull=True)
                .values_list("id", flat=True)
            )
            if not leaf_ids:
                break
            deleted = Location.objects.filter(id__in=leaf_ids).delete()
            n = deleted[1].get("locations.Location", 0)
            total += n
            all_ids.difference_update(leaf_ids)

        return total

    def _collect_subtree_ids(self, root_id):
        children_by_parent: dict[int | None, list[int]] = {}
        for loc_id, parent_id in Location.objects.values_list("id", "parent_id"):
            children_by_parent.setdefault(parent_id, []).append(loc_id)

        ids: set[int] = set()
        stack = [root_id]
        while stack:
            current = stack.pop()
            if current in ids:
                continue
            ids.add(current)
            stack.extend(children_by_parent.get(current, []))
        return ids
