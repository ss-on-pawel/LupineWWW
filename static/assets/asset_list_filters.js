(function () {
    function createRegistry(schema) {
        const fieldMap = {};

        (Array.isArray(schema) ? schema : []).forEach(function (item) {
            if (!item || !item.field || !item.type || !Array.isArray(item.operators)) {
                return;
            }

            fieldMap[item.field] = {
                field: item.field,
                label: item.label || item.field,
                type: item.type,
                operators: item.type === "enum"
                    ? item.operators.filter(function (operator) { return operator !== "in"; })
                    : item.operators.slice(),
                choices: Array.isArray(item.choices) ? item.choices.slice() : []
            };
        });

        return {
            fields: Object.keys(fieldMap).map(function (key) {
                return fieldMap[key];
            }),
            fieldMap: fieldMap
        };
    }

    function createFilterState(schema) {
        return {
            registry: createRegistry(schema),
            nextId: 1
        };
    }

    function getFieldDefinition(filterState, field) {
        return filterState.registry.fieldMap[field] || null;
    }

    function isAllowedOperator(definition, operator) {
        return Boolean(definition && definition.operators.indexOf(operator) !== -1);
    }

    function getDefaultOperator(definition) {
        return definition && definition.operators.length ? definition.operators[0] : "";
    }

    function getDefaultValue(definition, operator) {
        if (!definition) {
            return "";
        }

        if ((definition.type === "number" || definition.type === "date") && operator === "between") {
            return { from: "", to: "" };
        }

        return "";
    }

    function normalizeValue(definition, operator, value) {
        if (!definition) {
            return value;
        }

        if ((definition.type === "number" || definition.type === "date") && operator === "between") {
            const normalized = value && typeof value === "object" && !Array.isArray(value) ? value : {};
            return {
                from: normalized.from !== undefined ? normalized.from : "",
                to: normalized.to !== undefined ? normalized.to : ""
            };
        }

        if (Array.isArray(value)) {
            return value[0] || "";
        }

        if (value && typeof value === "object") {
            return "";
        }

        return value;
    }

    function resolveValueInputSpec(filterState, field, operator, value) {
        const definition = getFieldDefinition(filterState, field);
        if (!definition || !isAllowedOperator(definition, operator)) {
            return {
                kind: "single",
                control: "input",
                inputType: "text",
                value: "",
                placeholder: ""
            };
        }

        const normalizedValue = normalizeValue(definition, operator, value);

        if (definition.type === "enum") {
            return {
                kind: "single",
                control: "select",
                value: normalizedValue,
                placeholder: "Wybierz",
                options: definition.choices.slice()
            };
        }

        if ((definition.type === "number" || definition.type === "date") && operator === "between") {
            return {
                kind: "range",
                control: "range",
                valueType: definition.type,
                value: normalizedValue,
                placeholders: {
                    from: "od",
                    to: "do"
                }
            };
        }

        if (definition.type === "number") {
            return {
                kind: "single",
                control: "input",
                inputType: "number",
                value: normalizedValue,
                placeholder: "np. 1000",
                step: "any"
            };
        }

        if (definition.type === "date") {
            return {
                kind: "single",
                control: "input",
                inputType: "date",
                value: normalizedValue,
                placeholder: "YYYY-MM-DD"
            };
        }

        return {
            kind: "single",
            control: "input",
            inputType: "text",
            value: normalizedValue,
            placeholder: "Wpisz tekst..."
        };
    }

    function getValueShapeSignature(spec) {
        if (!spec) {
            return "";
        }

        if (spec.kind === "range") {
            return [spec.kind, spec.control, spec.valueType].join(":");
        }

        return [
            spec.kind,
            spec.control,
            spec.inputType || "",
            "single"
        ].join(":");
    }

    function shouldResetValue(currentSpec, nextSpec) {
        return getValueShapeSignature(currentSpec) !== getValueShapeSignature(nextSpec);
    }

    function createTextNode(value) {
        return document.createTextNode(value);
    }

    function renderValueControl(spec) {
        if (spec.kind === "range") {
            const wrapper = document.createElement("div");
            wrapper.className = "asset-filter-range";

            const fromInput = document.createElement("input");
            fromInput.className = "asset-filter-input";
            fromInput.dataset.role = "value-range-part";
            fromInput.dataset.part = "from";
            fromInput.setAttribute("aria-label", "Warto\u015b\u0107 od");
            fromInput.type = spec.valueType;
            fromInput.placeholder = spec.placeholders.from;
            fromInput.value = spec.value.from || "";
            if (spec.valueType === "number") {
                fromInput.step = "any";
            }

            const separator = document.createElement("span");
            separator.className = "asset-filter-range-separator";
            separator.setAttribute("aria-hidden", "true");
            separator.appendChild(createTextNode("-"));

            const toInput = document.createElement("input");
            toInput.className = "asset-filter-input";
            toInput.dataset.role = "value-range-part";
            toInput.dataset.part = "to";
            toInput.setAttribute("aria-label", "Warto\u015b\u0107 do");
            toInput.type = spec.valueType;
            toInput.placeholder = spec.placeholders.to;
            toInput.value = spec.value.to || "";
            if (spec.valueType === "number") {
                toInput.step = "any";
            }

            wrapper.appendChild(fromInput);
            wrapper.appendChild(separator);
            wrapper.appendChild(toInput);
            return wrapper;
        }

        if (spec.control === "select") {
            const select = document.createElement("select");
            select.className = "asset-filter-select";
            select.dataset.role = "value";
            select.setAttribute("aria-label", "Warto\u015b\u0107");

            const emptyOption = document.createElement("option");
            emptyOption.value = "";
            emptyOption.textContent = spec.placeholder || "Wybierz";
            select.appendChild(emptyOption);

            const selectedValues = Array.isArray(spec.value) ? spec.value : [spec.value];
            spec.options.forEach(function (choice) {
                const option = document.createElement("option");
                option.value = choice.value;
                option.textContent = choice.label;
                option.selected = selectedValues.indexOf(choice.value) !== -1;
                select.appendChild(option);
            });

            return select;
        }

        const input = document.createElement("input");
        input.className = "asset-filter-input";
        input.dataset.role = "value";
        input.type = spec.inputType || "text";
        input.value = Array.isArray(spec.value) ? "" : (spec.value || "");
        if (spec.placeholder) {
            input.placeholder = spec.placeholder;
        }
        if (spec.step) {
            input.step = spec.step;
        }
        return input;
    }

    function getOperatorOptions(filterState, field) {
        const definition = getFieldDefinition(filterState, field);
        if (!definition) {
            return [];
        }

        return definition.operators.map(function (operator) {
            return {
                value: operator,
                label: getOperatorLabel(operator)
            };
        });
    }

    function createCondition(filterState, seed) {
        const firstField = filterState.registry.fields[0];
        const field = seed && filterState.registry.fieldMap[seed.field] ? seed.field : (firstField ? firstField.field : "");
        const definition = filterState.registry.fieldMap[field] || firstField || null;
        const operator = seed && isAllowedOperator(definition, seed.operator)
            ? seed.operator
            : getDefaultOperator(definition);

        const id = seed && seed.id ? seed.id : "f" + filterState.nextId;
        filterState.nextId += 1;

        return {
            id: id,
            field: field,
            operator: operator,
            value: normalizeValue(
                definition,
                operator,
                seed && seed.value !== undefined ? seed.value : getDefaultValue(definition, operator)
            )
        };
    }

    function sanitizeConditions(filterState, conditions) {
        return (Array.isArray(conditions) ? conditions : []).map(function (condition) {
            return normalizeCondition(filterState, condition);
        });
    }

    function normalizeCondition(filterState, condition) {
        return createCondition(filterState, condition || {});
    }

    function isFilterActive(filterState, condition) {
        const spec = resolveValueInputSpec(filterState, condition.field, condition.operator, condition.value);

        if (spec.kind === "range") {
            return String(spec.value.from || "").trim() !== "" && String(spec.value.to || "").trim() !== "";
        }

        return String(spec.value || "").trim() !== "";
    }

    function getActiveFiltersCount(filterState, conditions) {
        return (Array.isArray(conditions) ? conditions : []).filter(function (condition) {
            return isFilterActive(filterState, condition);
        }).length;
    }

    function isFilterInvalid(filterState, condition) {
        const spec = resolveValueInputSpec(filterState, condition.field, condition.operator, condition.value);

        if (spec.kind !== "range" || !isFilterActive(filterState, condition)) {
            return false;
        }

        const fromValue = String(spec.value.from || "").trim();
        const toValue = String(spec.value.to || "").trim();

        if (spec.valueType === "number") {
            const fromNumber = Number.parseFloat(fromValue);
            const toNumber = Number.parseFloat(toValue);
            return !Number.isNaN(fromNumber) && !Number.isNaN(toNumber) && fromNumber > toNumber;
        }

        if (spec.valueType === "date") {
            return fromValue > toValue;
        }

        return false;
    }

    function getFilterUiState(filterState, condition) {
        if (isFilterInvalid(filterState, condition)) {
            return "invalid";
        }

        return isFilterActive(filterState, condition) ? "active" : "inactive";
    }

    function serializeConditions(filterState, conditions) {
        const params = new URLSearchParams();

        (Array.isArray(conditions) ? conditions : []).forEach(function (condition) {
            const serializedValue = serializeConditionValue(filterState, condition);
            if (serializedValue === null) {
                return;
            }

            params.set("filter__" + condition.field + "__" + condition.operator, serializedValue);
        });

        return params;
    }

    function serializeConditionValue(filterState, condition) {
        const definition = getFieldDefinition(filterState, condition.field);
        if (!definition || !isAllowedOperator(definition, condition.operator)) {
            return null;
        }

        const spec = resolveValueInputSpec(filterState, condition.field, condition.operator, condition.value);

        if (spec.kind === "range") {
            const fromValue = String(spec.value.from || "").trim();
            const toValue = String(spec.value.to || "").trim();
            return fromValue && toValue ? fromValue + "," + toValue : null;
        }

        if (Array.isArray(spec.value)) {
            return null;
        }

        const rawValue = spec.value === undefined || spec.value === null ? "" : String(spec.value).trim();
        return rawValue ? rawValue : null;
    }

    function getOperatorLabel(operator) {
        const labels = {
            contains: "zawiera",
            equals: "r\u00f3wna si\u0119",
            eq: "r\u00f3wne",
            gt: "wi\u0119ksze ni\u017c",
            lt: "mniejsze ni\u017c",
            between: "mi\u0119dzy",
            before: "przed",
            after: "po"
        };
        return labels[operator] || operator;
    }

    window.AssetListFilters = {
        createFilterState: createFilterState,
        createCondition: createCondition,
        sanitizeConditions: sanitizeConditions,
        normalizeCondition: normalizeCondition,
        getFieldDefinition: getFieldDefinition,
        getOperatorOptions: getOperatorOptions,
        resolveValueInputSpec: resolveValueInputSpec,
        renderValueControl: renderValueControl,
        serializeConditions: serializeConditions,
        serializeConditionValue: serializeConditionValue,
        isFilterActive: isFilterActive,
        getActiveFiltersCount: getActiveFiltersCount,
        isFilterInvalid: isFilterInvalid,
        getFilterUiState: getFilterUiState,
        isConditionComplete: isFilterActive,
        shouldResetValue: shouldResetValue,
        getOperatorLabel: getOperatorLabel,
        getDefaultOperator: getDefaultOperator,
        getDefaultValue: getDefaultValue
    };
})();
