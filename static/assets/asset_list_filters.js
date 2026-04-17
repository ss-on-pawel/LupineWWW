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
                operators: item.operators.slice(),
                choices: Array.isArray(item.choices) ? item.choices.slice() : []
            };
        });

        const fields = Object.keys(fieldMap).map(function (key) {
            return fieldMap[key];
        });

        return {
            fields: fields,
            fieldMap: fieldMap
        };
    }

    function createFilterState(schema) {
        const registry = createRegistry(schema);
        return {
            registry: registry,
            nextId: 1
        };
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
            value: normalizeValue(definition, operator, seed && seed.value !== undefined ? seed.value : getDefaultValue(definition, operator))
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

    function getFieldDefinition(filterState, field) {
        return filterState.registry.fieldMap[field] || null;
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

    function getValueInputConfig(filterState, field, operator) {
        const definition = getFieldDefinition(filterState, field);
        if (!definition) {
            return { mode: "text", inputType: "text", multiple: false, choices: [] };
        }

        if (definition.type === "enum") {
            return {
                mode: "enum",
                inputType: "select",
                multiple: operator === "in",
                choices: definition.choices
            };
        }

        if (definition.type === "number") {
            if (operator === "between") {
                return { mode: "number-range", inputType: "number", multiple: false, choices: [], isRange: true };
            }
            return { mode: "number", inputType: "number", multiple: false, choices: [] };
        }

        if (definition.type === "date") {
            if (operator === "between") {
                return { mode: "date-range", inputType: "date", multiple: false, choices: [], isRange: true };
            }
            return { mode: "date", inputType: "date", multiple: false, choices: [] };
        }

        return { mode: "text", inputType: "text", multiple: false, choices: [] };
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

        if (definition.type === "enum" && condition.operator === "in") {
            const values = Array.isArray(condition.value)
                ? condition.value.map(function (item) { return String(item).trim(); }).filter(Boolean)
                : [];
            return values.length ? values.join(",") : null;
        }

        if (condition.operator === "between") {
            const fromValue = condition.value && condition.value.from !== undefined
                ? String(condition.value.from).trim()
                : "";
            const toValue = condition.value && condition.value.to !== undefined
                ? String(condition.value.to).trim()
                : "";
            return fromValue && toValue ? fromValue + "," + toValue : null;
        }

        if (Array.isArray(condition.value)) {
            return null;
        }

        const rawValue = condition.value === undefined || condition.value === null ? "" : String(condition.value).trim();
        return rawValue ? rawValue : null;
    }

    function getDefaultOperator(definition) {
        return definition && definition.operators.length ? definition.operators[0] : "";
    }

    function getDefaultValue(definition, operator) {
        if (!definition) {
            return "";
        }
        if (definition.type === "enum" && operator === "in") {
            return [];
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

        if (definition.type === "enum" && operator === "in") {
            return Array.isArray(value) ? value : (value ? [value] : []);
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

    function isAllowedOperator(definition, operator) {
        return Boolean(definition && definition.operators.indexOf(operator) !== -1);
    }

    function getOperatorLabel(operator) {
        const labels = {
            contains: "zawiera",
            equals: "równa się",
            in: "należy do",
            eq: "równe",
            gt: "większe niż",
            lt: "mniejsze niż",
            between: "między",
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
        getValueInputConfig: getValueInputConfig,
        serializeConditions: serializeConditions,
        getOperatorLabel: getOperatorLabel,
        getDefaultOperator: getDefaultOperator,
        getDefaultValue: getDefaultValue
    };
})();
