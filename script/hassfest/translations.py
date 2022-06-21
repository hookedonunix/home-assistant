"""Validate integration translation files."""
from __future__ import annotations

from functools import partial
from itertools import chain
import json
import re

import voluptuous as vol
from voluptuous.humanize import humanize_error

import homeassistant.helpers.config_validation as cv
from homeassistant.util import slugify
from script.translations import upload

from .model import Config, Integration

UNDEFINED = 0
REQUIRED = 1
REMOVED = 2

RE_REFERENCE = r"\[\%key:(.+)\%\]"

# Only allow translatino of integration names if they contain non-brand names
ALLOW_NAME_TRANSLATION = {
    "cert_expiry",
    "cpuspeed",
    "emulated_roku",
    "faa_delays",
    "garages_amsterdam",
    "google_travel_time",
    "homekit_controller",
    "islamic_prayer_times",
    "local_ip",
    "nmap_tracker",
    "rpi_power",
    "waze_travel_time",
}

REMOVED_TITLE_MSG = (
    "config.title key has been moved out of config and into the root of strings.json. "
    "Starting Home Assistant 0.109 you only need to define this key in the root "
    "if the title needs to be different than the name of your integration in the "
    "manifest."
)

MOVED_TRANSLATIONS_DIRECTORY_MSG = (
    "The '.translations' directory has been moved, the new name is 'translations', "
    "starting with Home Assistant 0.112 your translations will no longer "
    "load if you do not move/rename this "
)


def allow_name_translation(integration: Integration):
    """Validate that the translation name is not the same as the integration name."""
    # Only enforce for core because custom integrations can't be
    # added to allow list.
    return integration.core and (
        integration.domain in ALLOW_NAME_TRANSLATION
        or integration.quality_scale == "internal"
    )


def check_translations_directory_name(integration: Integration) -> None:
    """Check that the correct name is used for the translations directory."""
    legacy_translations = integration.path / ".translations"
    translations = integration.path / "translations"

    if translations.is_dir():
        # No action required
        return

    if legacy_translations.is_dir():
        integration.add_error("translations", MOVED_TRANSLATIONS_DIRECTORY_MSG)


def find_references(strings, prefix, found):
    """Find references."""
    for key, value in strings.items():
        if isinstance(value, dict):
            find_references(value, f"{prefix}::{key}", found)
            continue

        match = re.match(RE_REFERENCE, value)

        if match:
            found.append({"source": f"{prefix}::{key}", "ref": match.groups()[0]})


def removed_title_validator(config, integration, value):
    """Mark removed title."""
    if not config.specific_integrations:
        raise vol.Invalid(REMOVED_TITLE_MSG)

    # Don't mark it as an error yet for custom components to allow backwards compat.
    integration.add_warning("translations", REMOVED_TITLE_MSG)
    return value


def lowercase_validator(value):
    """Validate value is lowercase."""
    if value.lower() != value:
        raise vol.Invalid("Needs to be lowercase")

    return value


def gen_data_entry_schema(
    *,
    config: Config,
    integration: Integration,
    flow_title: int,
    require_step_title: bool,
    mandatory_description: str | None = None,
):
    """Generate a data entry schema."""
    step_title_class = vol.Required if require_step_title else vol.Optional
    schema = {
        vol.Optional("flow_title"): cv.string_with_no_html,
        vol.Required("step"): {
            str: {
                step_title_class("title"): cv.string_with_no_html,
                vol.Optional("description"): cv.string_with_no_html,
                vol.Optional("data"): {str: cv.string_with_no_html},
                vol.Optional("data_description"): {str: cv.string_with_no_html},
                vol.Optional("menu_options"): {str: cv.string_with_no_html},
            }
        },
        vol.Optional("error"): {str: cv.string_with_no_html},
        vol.Optional("abort"): {str: cv.string_with_no_html},
        vol.Optional("progress"): {str: cv.string_with_no_html},
        vol.Optional("create_entry"): {str: cv.string_with_no_html},
    }
    if flow_title == REQUIRED:
        schema[vol.Required("title")] = cv.string_with_no_html
    elif flow_title == REMOVED:
        schema[vol.Optional("title", msg=REMOVED_TITLE_MSG)] = partial(
            removed_title_validator, config, integration
        )

    def data_description_validator(value):
        """Validate data description."""
        for step_info in value["step"].values():
            if "data_description" not in step_info:
                continue

            for key in step_info["data_description"]:
                if key not in step_info["data"]:
                    raise vol.Invalid(f"data_description key {key} is not in data")

        return value

    validators = [vol.Schema(schema), data_description_validator]

    if mandatory_description is not None:

        def validate_description_set(value):
            """Validate description is set."""
            steps = value["step"]
            if mandatory_description not in steps:
                raise vol.Invalid(f"{mandatory_description} needs to be defined")

            if "description" not in steps[mandatory_description]:
                raise vol.Invalid(f"Step {mandatory_description} needs a description")

            return value

        validators.append(validate_description_set)

    if not allow_name_translation(integration):

        def name_validator(value):
            """Validate name."""
            for step_id, info in value["step"].items():
                if info.get("title") == integration.name:
                    raise vol.Invalid(
                        f"Do not set title of step {step_id} if it's a brand name "
                        "or add exception to ALLOW_NAME_TRANSLATION"
                    )

            return value

        validators.append(name_validator)

    return vol.All(*validators)


def gen_strings_schema(config: Config, integration: Integration):
    """Generate a strings schema."""
    return vol.Schema(
        {
            vol.Optional("title"): cv.string_with_no_html,
            vol.Optional("config"): gen_data_entry_schema(
                config=config,
                integration=integration,
                flow_title=REMOVED,
                require_step_title=False,
                mandatory_description=(
                    "user" if integration.integration_type == "helper" else None
                ),
            ),
            vol.Optional("options"): gen_data_entry_schema(
                config=config,
                integration=integration,
                flow_title=UNDEFINED,
                require_step_title=False,
            ),
            vol.Optional("device_automation"): {
                vol.Optional("action_type"): {str: cv.string_with_no_html},
                vol.Optional("condition_type"): {str: cv.string_with_no_html},
                vol.Optional("trigger_type"): {str: cv.string_with_no_html},
                vol.Optional("trigger_subtype"): {str: cv.string_with_no_html},
            },
            vol.Optional("state"): cv.schema_with_slug_keys(
                cv.schema_with_slug_keys(str, slug_validator=lowercase_validator),
                slug_validator=vol.Any("_", cv.slug),
            ),
            vol.Optional("system_health"): {
                vol.Optional("info"): {str: cv.string_with_no_html}
            },
            vol.Optional("config_panel"): cv.schema_with_slug_keys(
                cv.schema_with_slug_keys(
                    cv.string_with_no_html, slug_validator=lowercase_validator
                ),
                slug_validator=vol.Any("_", cv.slug),
            ),
            vol.Optional("application_credentials"): {
                vol.Optional("description"): cv.string_with_no_html,
            },
        }
    )


def gen_auth_schema(config: Config, integration: Integration):
    """Generate auth schema."""
    return vol.Schema(
        {
            vol.Optional("mfa_setup"): {
                str: gen_data_entry_schema(
                    config=config,
                    integration=integration,
                    flow_title=REQUIRED,
                    require_step_title=True,
                )
            }
        }
    )


def gen_platform_strings_schema(config: Config, integration: Integration):
    """Generate platform strings schema like strings.sensor.json.

    Example of valid data:
    {
        "state": {
            "moon__phase": {
                "full": "Full"
            }
        }
    }
    """

    def device_class_validator(value):
        """Key validator for platform states.

        Platform states are only allowed to provide states for device classes they prefix.
        """
        if not value.startswith(f"{integration.domain}__"):
            raise vol.Invalid(
                f"Device class need to start with '{integration.domain}__'. Key {value} is invalid. See https://developers.home-assistant.io/docs/internationalization/core#stringssensorjson"
            )

        slug_friendly = value.replace("__", "_", 1)
        slugged = slugify(slug_friendly)

        if slug_friendly != slugged:
            raise vol.Invalid(
                f"invalid device class {value}. After domain__, needs to be all lowercase, no spaces."
            )

        return value

    return vol.Schema(
        {
            vol.Optional("state"): cv.schema_with_slug_keys(
                cv.schema_with_slug_keys(str, slug_validator=lowercase_validator),
                slug_validator=device_class_validator,
            )
        }
    )


ONBOARDING_SCHEMA = vol.Schema({vol.Required("area"): {str: cv.string_with_no_html}})


def validate_translation_file(config: Config, integration: Integration, all_strings):
    """Validate translation files for integration."""
    if config.specific_integrations:
        check_translations_directory_name(integration)

    strings_files = [integration.path / "strings.json"]

    # Also validate translations for custom integrations
    if config.specific_integrations:
        # Only English needs to be always complete
        strings_files.append(integration.path / "translations/en.json")

    references = []

    if integration.domain == "auth":
        strings_schema = gen_auth_schema(config, integration)
    elif integration.domain == "onboarding":
        strings_schema = ONBOARDING_SCHEMA
    elif integration.domain == "binary_sensor":
        strings_schema = gen_strings_schema(config, integration).extend(
            {
                vol.Optional("device_class"): cv.schema_with_slug_keys(
                    cv.string_with_no_html, slug_validator=vol.Any("_", cv.slug)
                )
            }
        )
    else:
        strings_schema = gen_strings_schema(config, integration)

    for strings_file in strings_files:
        if not strings_file.is_file():
            continue

        name = str(strings_file.relative_to(integration.path))

        try:
            strings = json.loads(strings_file.read_text())
        except ValueError as err:
            integration.add_error("translations", f"Invalid JSON in {name}: {err}")
            continue

        try:
            strings_schema(strings)
        except vol.Invalid as err:
            integration.add_error(
                "translations", f"Invalid {name}: {humanize_error(strings, err)}"
            )
        else:
            if strings_file.name == "strings.json":
                find_references(strings, name, references)

                if strings.get(
                    "title"
                ) == integration.name and not allow_name_translation(integration):
                    integration.add_error(
                        "translations",
                        "Don't specify title in translation strings if it's a brand name "
                        "or add exception to ALLOW_NAME_TRANSLATION",
                    )

    platform_string_schema = gen_platform_strings_schema(config, integration)
    platform_strings = [integration.path.glob("strings.*.json")]

    if config.specific_integrations:
        platform_strings.append(integration.path.glob("translations/*.en.json"))

    for path in chain(*platform_strings):
        name = str(path.relative_to(integration.path))

        try:
            strings = json.loads(path.read_text())
        except ValueError as err:
            integration.add_error("translations", f"Invalid JSON in {name}: {err}")
            continue

        try:
            platform_string_schema(strings)
        except vol.Invalid as err:
            msg = f"Invalid {path.name}: {humanize_error(strings, err)}"
            if config.specific_integrations:
                integration.add_warning("translations", msg)
            else:
                integration.add_error("translations", msg)
        else:
            find_references(strings, path.name, references)

    if config.specific_integrations:
        return

    # Validate references
    for reference in references:
        parts = reference["ref"].split("::")
        search = all_strings
        key = parts.pop(0)
        while parts and key in search:
            search = search[key]
            key = parts.pop(0)

        if parts or key not in search:
            integration.add_error(
                "translations",
                f"{reference['source']} contains invalid reference {reference['ref']}: Could not find {key}",
            )


def validate(integrations: dict[str, Integration], config: Config):
    """Handle JSON files inside integrations."""
    if config.specific_integrations:
        all_strings = None
    else:
        all_strings = upload.generate_upload_data()

    for integration in integrations.values():
        validate_translation_file(config, integration, all_strings)
