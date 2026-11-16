from assets.models import Asset, AssetTypeDictionary


UNASSIGNED_ASSET_TYPE = ""
UNASSIGNED_ASSET_TYPE_LABEL = "Bez przypisanego rodzaju"


def get_inventory_asset_type_choices():
    choices = dict(Asset.AssetType.choices)
    choices[Asset.AssetType.LOW_VALUE] = "Niskocenny"
    choices[Asset.AssetType.QUANTITY] = "Ilościowy"
    # Include customer dictionaries, including types still used after deactivation.
    for code, name in AssetTypeDictionary.objects.order_by("sort_order", "name").values_list("code", "name"):
        choices.setdefault(code, name)
    choices[UNASSIGNED_ASSET_TYPE] = UNASSIGNED_ASSET_TYPE_LABEL
    return list(choices.items())
