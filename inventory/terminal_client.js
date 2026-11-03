/* Local scans and working state are committed together before confirmation. */
(function () {
    "use strict";
    const config = JSON.parse(document.getElementById("terminal-data").textContent);
    const el = name => document.querySelector("[data-" + name + "]");
    const els = {
        status: el("online-status"), setupMessage: el("message"), scanMessage: el("scan-message"),
        setup: el("setup-panel"), scan: el("scan-panel"), sessions: el("session-select"),
        name: el("terminal-name"), input: el("scan-input"), count: el("local-count"),
        pending: el("pending-count"), location: el("current-location"), last: el("last-scan"),
        session: el("current-session"), terminal: el("current-terminal"), memory: el("memory-status"),
        login: el("login-link"), retry: el("retry-save"), start: el("start-button")
    };
    const DB_NAME = "lupine-terminal-v1";
    let db, state = {}, locationsByCode = {}, syncing = false, writing = false, backingUp = false;
    let readyOffline = false, persistent = false, writeFailed = false, accountMismatch = false, serverReachable = null;
    const writes = [];
    let localLock = {}, finishingLogout = false, activeAccount = null, backupUrl = null;
    class AuthError extends Error {}
    function message(text, error = false, target = null) {
        const node = target || (els.scan.classList.contains("terminal-hidden") ? els.setupMessage : els.scanMessage);
        node.textContent = text;
        node.className = "terminal-message" + (error ? " is-error" : "");
    }
    function tx(stores, mode) {
        try { return db.transaction(stores, mode, {durability: "strict"}); }
        catch (_) { return db.transaction(stores, mode); }
    }
    function committed(stores, action) {
        return new Promise((resolve, reject) => {
            const transaction = tx(stores, "readwrite");
            transaction.oncomplete = resolve;
            transaction.onabort = () => reject(transaction.error || new Error("Zapis został przerwany."));
            transaction.onerror = () => {}; // onabort is the final outcome
            try { action(transaction); } catch (error) { transaction.abort(); reject(error); }
        });
    }
    function read(store, key) {
        return new Promise((resolve, reject) => {
            const request = tx([store], "readonly").objectStore(store).get(key);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }
    // History is queried only for an explicit export. Normal work uses indexes.
    function indexCount(index, key) {
        return new Promise((resolve, reject) => {
            const request = tx(["scans"], "readonly").objectStore("scans").index(index).count(key);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }
    function pendingBatch(owner, blocked) {
        return new Promise((resolve, reject) => {
            const index = tx(["scans"], "readonly").objectStore("scans").index("pending_owner_group");
            const range = IDBKeyRange.bound([1, owner], [1, owner, []]);
            const request = index.openCursor(range);
            const batch = [];
            let group = null;
            request.onerror = () => reject(request.error);
            request.onsuccess = () => {
                const cursor = request.result;
                if (!cursor) return resolve(batch);
                const scan = cursor.value;
                const key = scan.session_id + ":" + scan.terminal_id;
                if (blocked.has(key)) {
                    cursor.continue([1, owner, scan.session_id, scan.terminal_id, []]);
                    return;
                }
                if (group !== null && group !== key) return resolve(batch);
                group = key;
                batch.push(scan);
                if (batch.length === 100) return resolve(batch);
                cursor.continue();
            };
        });
    }

    function openDB() {
        return new Promise((resolve, reject) => {
            const request = indexedDB.open(DB_NAME, 3);
            request.onupgradeneeded = event => {
                const database = request.result;
                const transaction = request.transaction;
                const store = database.objectStoreNames.contains("scans") ? transaction.objectStore("scans") : database.createObjectStore("scans", {keyPath: "client_scan_id"});
                const meta = database.objectStoreNames.contains("meta") ? transaction.objectStore("meta") : database.createObjectStore("meta", {keyPath: "key"});
                store.createIndex("pending", "pending");
                store.createIndex("pending_owner", ["pending", "owner_id"]);
                store.createIndex("pending_owner_group", ["pending", "owner_id", "session_id", "terminal_id"]);
                store.createIndex("owner_group", ["owner_id", "session_id", "terminal_id"]);
                if (event.oldVersion > 0) {
                    message("Przygotowanie indeksów pamięci. Zachowuję dotychczasowe skany...");
                    const cursor = store.openCursor();
                    cursor.onsuccess = () => {
                        const item = cursor.result;
                        if (!item) return;
                        item.update({...item.value, owner_id: item.value.owner_id || 0, pending: item.value.synced ? 0 : 1});
                        item.continue();
                    };
                    const saved = meta.get("state");
                    saved.onsuccess = () => {
                        if (!saved.result || !saved.result.value.locations) return;
                        const {locations, ...workingState} = saved.result.value;
                        meta.put({key: "locations", value: locations});
                        meta.put({key: "state", value: workingState});
                    };
                }
            };
            request.onblocked = () => message("Zamknij inne karty terminala, aby przygotować pamięć offline.", true);
            request.onerror = () => reject(request.error);
            request.onsuccess = () => {
                request.result.onversionchange = () => {
                    request.result.close(); els.input.readOnly = true;
                    message("Pamięć zaktualizowano w innej karcie. Otwórz terminal ponownie.", true);
                };
                resolve(request.result);
            };
        });
    }
    function putState(next, locations) {
        return committed(["meta"], transaction => {
            const store = transaction.objectStore("meta");
            store.put({key: "state", value: next});
            store.put({key: "locations", value: locations});
        });
    }
    function url(template, values) {
        Object.keys(values).forEach(key => { template = template.replace("__" + key + "__", String(values[key])); });
        return template;
    }
    async function json(url, options = {}) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {...options, cache: "no-store", signal: controller.signal});
            serverReachable = true; connectivity();
            if (response.redirected || response.status === 401 || response.status === 403) {
                const error = new AuthError("Zaloguj się ponownie, aby synchronizować. Skany pozostają w pamięci.");
                error.loggedOut = response.redirected || response.status === 401;
                throw error;
            }
            if (!(response.headers.get("Content-Type") || "").includes("application/json")) throw new Error("Brak odpowiedzi serwera. Skany pozostają w pamięci.");
            const data = await response.json();
            if (!response.ok && !Array.isArray(data.accepted)) throw new Error(data.error || "Serwer odrzucił synchronizację. Skany pozostają w pamięci.");
            return data;
        } catch (error) {
            if (error instanceof TypeError || error.name === "AbortError") {
                serverReachable = false; connectivity();
                throw new Error("Brak połączenia z serwerem. Skany pozostają w pamięci.");
            }
            throw error;
        } finally { clearTimeout(timeout); }
    }
    async function context() {
        const data = await json(config.context_url);
        if (!data.ok) throw new AuthError("Zaloguj się ponownie.");
        activeAccount = data.user.id;
        if (localLock.logout_pending) throw new AuthError("Poczekaj na dokończenie wylogowania z serwera.");
        if (state.owner_id && state.owner_id !== data.user.id) {
            accountMismatch = true;
            els.input.readOnly = true;
            throw new AuthError("Ta pamięć należy do " + state.owner_name + ". Zaloguj się na to konto; kolejka nie została wysłana.");
        }
        accountMismatch = false;
        if (localLock.locked) {
            await saveLock({locked: false, logout_pending: false});
            if (state.session_id) showScan(); else els.setup.classList.remove("terminal-hidden");
        }
        el("account-message").textContent = "";
        el("resume-button").hidden = true;
        els.login.hidden = true;
        if (!writeFailed && !backingUp) els.input.readOnly = false;
        return data;
    }
    function reportError(error) {
        if (error instanceof AuthError) { els.login.hidden = false; el("resume-button").hidden = false; }
        message(error.message || "Błąd. Skany pozostają w pamięci.", true);
    }
    function connectivity() {
        const online = navigator.onLine && serverReachable !== false;
        els.status.textContent = online ? "Online" : "Offline";
        els.status.className = "terminal-status " + (online ? "is-online" : "is-offline");
    }
    function showScan() {
        if (localLock.locked) return;
        els.setup.classList.add("terminal-hidden"); els.scan.classList.remove("terminal-hidden");
        els.session.textContent = state.session_number || "Sesja";
        els.terminal.textContent = state.terminal_name || "Terminal";
        els.location.textContent = state.current_location_label || "-";
        els.last.textContent = state.last_scan || "-";
        els.count.textContent = String(state.local_count || 0);
        els.input.focus();
    }
    async function counters() {
        const [pending, legacy] = await Promise.all([indexCount("pending", 1), indexCount("pending_owner", [1, 0])]);
        els.pending.textContent = String(pending);
        els.count.textContent = String(state.local_count || 0);
        if (legacy) message("W pamięci są starsze skany bez konta. Zachowano je; pobierz kopię przed dalszym odzyskiwaniem.", true);
    }
    async function storageStatus() {
        let suffix = "";
        try {
            persistent = Boolean(navigator.storage && await navigator.storage.persisted());
            const estimate = navigator.storage && await navigator.storage.estimate();
            if (estimate && estimate.quota && estimate.usage / estimate.quota > 0.85) suffix = " Mało wolnego miejsca — zsynchronizuj i pobierz kopię.";
        } catch (_) {}
        els.memory.textContent = (readyOffline ? "Ponowne otwarcie offline: gotowe. " : "Ponowne otwarcie offline: nieprzygotowane. ") +
            (persistent ? "Pamięć trwała." : "Przeglądarka nie potwierdziła ochrony pamięci przed automatycznym czyszczeniem.") + suffix;
    }
    async function prepareOffline() {
        if (!navigator.serviceWorker || !window.isSecureContext) return storageStatus();
        try {
            const registration = await navigator.serviceWorker.register(config.worker_url, {scope: config.app_url});
            await Promise.race([navigator.serviceWorker.ready, new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 15000))]);
            const cache = await caches.open(config.shell_cache);
            readyOffline = Boolean(await cache.match(config.offline_url) && await cache.match(config.client_url));
            if (!readyOffline && navigator.onLine) {
                const response = await fetch(config.offline_url, {cache: "no-store"});
                if (response.ok && !response.redirected) await cache.put(config.offline_url, response);
                const client = await fetch(config.client_url, {cache: "no-store"});
                if (client.ok && !client.redirected) await cache.put(config.client_url, client);
                readyOffline = Boolean(await cache.match(config.offline_url) && await cache.match(config.client_url));
            }
            void registration;
        } catch (_) { readyOffline = false; }
        await storageStatus();
    }
    async function loadSessions() {
        if (!navigator.onLine) {
            message(state.owner_id ? "Praca offline. Skany są zapisywane lokalnie." : "Pierwsze uruchomienie wymaga internetu.");
            return;
        }
        await context();
        const data = await json(config.sessions_url);
        els.sessions.textContent = "";
        (data.sessions || []).forEach(session => {
            const option = document.createElement("option"); option.value = session.id;
            option.textContent = session.number + (session.root_locations ? " - " + session.root_locations : "");
            els.sessions.appendChild(option);
        });
        if (state.session_id) els.sessions.value = String(state.session_id);
        els.name.value = state.terminal_name || "";
        if (els.scan.classList.contains("terminal-hidden")) message(data.sessions.length ? "Wybierz inwentaryzację i terminal." : "Brak aktywnych inwentaryzacji.");
    }
    async function start() {
        if (localLock.locked) throw new AuthError("Zaloguj się, aby wznowić pracę.");
        if (writing || writes.length || backingUp) throw new Error("Najpierw dokończ zapis skanów.");
        if (!navigator.onLine) throw new Error("Wybór nowej sesji wymaga internetu. Poprzednia sesja działa offline.");
        const sessionId = Number.parseInt(els.sessions.value, 10), name = els.name.value.trim();
        if (!sessionId || !name) throw new Error("Wybierz sesję i wpisz nazwę terminala.");
        els.start.disabled = true;
        try {
            const identity = await context();
            const bootstrap = await json(url(config.bootstrap_url_template, {SESSION_ID: sessionId}));
            const created = await json(url(config.terminal_create_url_template, {SESSION_ID: sessionId}), {
                method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": identity.csrf_token}, body: JSON.stringify({name})
            });
            const same = state.owner_id === identity.user.id && state.session_id === sessionId && state.terminal_id === created.terminal.id;
            const savedCount = same ? state.local_count : await indexCount("owner_group", [identity.user.id, sessionId, created.terminal.id]);
            const locations = {};
            bootstrap.locations.forEach(location => { locations[location.code] = location; });
            const next = {owner_id: identity.user.id, owner_name: identity.user.username,
                session_id: sessionId, session_number: bootstrap.session.number,
                terminal_id: created.terminal.id, terminal_name: created.terminal.name,
                local_count: savedCount,
                last_scan: same ? state.last_scan : "-",
                current_location_code: same ? state.current_location_code : "",
                current_location_label: same ? state.current_location_label : "-"};
            await putState(next, locations); state = next; locationsByCode = locations;
            if (navigator.storage && navigator.storage.persist) { try { await navigator.storage.persist(); } catch (_) {} }
            await prepareOffline(); showScan(); await counters();
            message("Gotowy. Każdy skan jest potwierdzany po zapisie w pamięci.");
            void sync();
        } finally { els.start.disabled = false; }
    }
    function id() {
        return window.crypto.randomUUID ? window.crypto.randomUUID() : Date.now().toString(36) + "-" + Array.from(window.crypto.getRandomValues(new Uint32Array(4))).join("-");
    }
    async function drain() {
        if (writing) return;
        writing = true;
        try {
            while (writes.length) {
                const event = writes[0];
                const location = locationsByCode[event.code];
                const next = {...state, local_count: Number(state.local_count || 0) + 1, last_scan: event.code};
                if (location) { next.current_location_code = event.code; next.current_location_label = location.path || location.name; }
                const record = {...event, owner_id: state.owner_id, session_id: state.session_id,
                    terminal_id: state.terminal_id, current_location_code: next.current_location_code || "", synced: false, pending: 1};
                await committed(["scans", "meta"], transaction => {
                    transaction.objectStore("scans").add(record);
                    transaction.objectStore("meta").put({key: "state", value: next});
                });
                state = next; writes.shift();
                els.last.textContent = state.last_scan; els.location.textContent = state.current_location_label || "-";
                els.count.textContent = String(state.local_count);
                message("Zapisano w pamięci.");
            }
            writeFailed = false; els.retry.hidden = true;
            els.input.readOnly = accountMismatch || backingUp || localLock.locked;
            await counters();
        } catch (error) {
            if (!writes.length) { reportError(error); return; }
            writeFailed = true; els.retry.hidden = false; els.input.readOnly = true;
            message("NIE ZAPISANO kodu " + writes[0].code + ". Nie zamykaj strony. Sprawdź wolne miejsce i naciśnij „Ponów zapis” lub pobierz kopię.", true);
        } finally { writing = false; }
        if (!writeFailed && navigator.onLine) void sync();
    }
    async function acknowledge(accepted, pending) {
        const ids = new Map(pending.map(scan => [scan.client_scan_id, scan]));
        await committed(["scans"], transaction => {
            const store = transaction.objectStore("scans");
            accepted.forEach(item => {
                const scan = ids.get(item.client_scan_id);
                if (scan && Number.isInteger(item.raw_scan_id)) store.put({...scan, synced: true, pending: 0,
                    raw_scan_id: item.raw_scan_id, synced_at: new Date().toISOString()});
            });
        });
    }
    async function sync() {
        if (db && localLock.logout_pending) return finishLogout();
        if (localLock.locked) return;
        if (!db || syncing || !navigator.onLine || !state.owner_id || writeFailed) return;
        syncing = true;
        try {
            const blocked = new Set();
            let batch = await pendingBatch(state.owner_id, blocked);
            if (!batch.length) return;
            const identity = await context();
            let lastError = null;
            while (batch.length && !localLock.locked) {
                const first = batch[0];
                const group = scan => scan.session_id + ":" + scan.terminal_id;
                try {
                    const data = await json(url(config.sync_url_template, {SESSION_ID: first.session_id, TERMINAL_ID: first.terminal_id}), {
                        method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": identity.csrf_token}, body: JSON.stringify({scans: batch})
                    });
                    const accepted = Array.isArray(data.accepted) ? data.accepted : [];
                    await acknowledge(accepted, batch); await counters();
                    if (data.errors && data.errors.length) throw new Error("Część skanów odrzucono. Pozostały w pamięci; pobierz kopię.");
                    if (!accepted.some(item => batch.some(scan => scan.client_scan_id === item.client_scan_id) && Number.isInteger(item.raw_scan_id))) throw new Error("Brak potwierdzeń serwera. Kolejka pozostała w pamięci.");
                } catch (error) {
                    if (error instanceof AuthError) throw error;
                    blocked.add(group(first)); lastError = error;
                }
                batch = await pendingBatch(state.owner_id, blocked);
            }
            if (lastError) throw lastError;
            if (!writeFailed && !localLock.locked) message("Zsynchronizowano. Kopia skanów pozostaje na tym urządzeniu.");
        } catch (error) { reportError(error); }
        finally { syncing = false; }
    }
    async function backup() {
        if (localLock.locked) throw new AuthError("Zaloguj się przed pobraniem kopii.");
        if (backingUp) return;
        backingUp = true;
        const button = el("backup-button");
        button.disabled = true; els.input.readOnly = true;
        try {
            const filename = "lupine-skany-" + new Date().toISOString().replace(/[:.]/g, "-") + ".txt";
            // Pick the destination during the user's gesture, before asynchronous reads.
            let fileHandle = null;
            if (typeof window.showSaveFilePicker === "function") {
                try {
                    fileHandle = await window.showSaveFilePicker({suggestedName: filename,
                        types: [{description: "Kopia skanów TXT", accept: {"text/plain": [".txt"]}}]});
                } catch (error) {
                    if (error.name === "AbortError") throw error;
                    // Some Android/browser policies expose the picker but deny it.
                    // A normal download with a persistent tap link remains available.
                }
            }
            // Finish any in-flight commit before freezing the export snapshot.
            while (writing) await new Promise(resolve => setTimeout(resolve, 10));
            message("Przygotowuję kopię skanów. Skanowanie wznowi się po jej pobraniu.");
            const metadata = {format: "lupine-terminal-backup-v2", exported_at: new Date().toISOString(), origin: location.origin,
                state: {...state, locations: locationsByCode}, not_committed: writes.slice()};
            const pieces = [JSON.stringify(metadata, null, 2).slice(0, -1), ',\n"scans":[\n'];
            await new Promise((resolve, reject) => {
                const transaction = tx(["scans"], "readonly");
                const store = transaction.objectStore("scans");
                let first = true;
                transaction.oncomplete = resolve;
                transaction.onabort = () => reject(transaction.error || new Error("Nie udało się odczytać kopii."));
                transaction.onerror = () => {};
                function chunk(after) {
                    const query = after === undefined ? null : IDBKeyRange.lowerBound(after, true);
                    const request = store.getAll(query, 500);
                    request.onsuccess = () => {
                        const records = request.result;
                        if (!records.length) return;
                        pieces.push((first ? "" : ",\n") + records.map(record => JSON.stringify(record)).join(",\n"));
                        first = false;
                        chunk(records[records.length - 1].client_scan_id);
                    };
                }
                chunk();
            });
            pieces.push("]}");
            const blob = new Blob(pieces, {type: "text/plain;charset=utf-8"});
            if (backupUrl) URL.revokeObjectURL(backupUrl);
            backupUrl = URL.createObjectURL(blob);
            const link = el("backup-download");
            link.href = backupUrl; link.download = filename; link.hidden = false;
            if (fileHandle) {
                const writable = await fileHandle.createWritable();
                try { await writable.write(blob); await writable.close(); }
                catch (error) { try { await writable.abort(); } catch (_) {} throw error; }
                message("Zapisano kopię TXT. Możesz kontynuować skanowanie.");
            } else {
                // Keep a real, attached link available for Android's direct tap gesture.
                link.click();
                message("Plik TXT gotowy. Jeśli pobieranie nie ruszyło, dotknij „Zapisz plik TXT”.");
            }
        } catch (error) {
            if (error.name === "AbortError") message("Anulowano zapis kopii. Skany pozostają w pamięci.");
            else throw error;
        } finally {
            backingUp = false; button.disabled = false;
            els.input.readOnly = writeFailed || accountMismatch || localLock.locked;
        }
    }

    function lockScreen() {
        const locked = Boolean(localLock.locked);
        els.input.readOnly = locked || writeFailed || accountMismatch || backingUp;
        els.start.disabled = locked;
        el("backup-button").disabled = locked || backingUp;
        el("logout-button").disabled = locked;
        if (locked) {
            els.setup.classList.add("terminal-hidden"); els.scan.classList.add("terminal-hidden");
            els.session.textContent = "Terminal"; els.terminal.textContent = "Wylogowano";
            els.login.hidden = false; el("resume-button").hidden = false;
            el("backup-download").hidden = true;
        }
    }
    async function saveLock(next) {
        await committed(["meta"], transaction => transaction.objectStore("meta").put({key: "lock", value: next}));
        localLock = next; lockScreen();
    }
    async function finishLogout() {
        if (!localLock.logout_pending || finishingLogout || !navigator.onLine) return;
        finishingLogout = true;
        try {
            let identity;
            try { identity = await json(config.context_url); }
            catch (error) { if (!(error instanceof AuthError && error.loggedOut)) throw error; }
            const differentAccount = identity && localLock.owner_id && identity.user.id !== localLock.owner_id;
            // Do not log out a different account that signed in meanwhile.
            if (identity && (!localLock.owner_id || identity.user.id === localLock.owner_id)) {
                await json(config.logout_url, {method: "POST", headers: {"X-CSRFToken": identity.csrf_token}});
            }
            await saveLock({...localLock, logout_pending: false});
            message(differentAccount ? "Inne konto jest zalogowane w przeglądarce. Terminal pozostaje zablokowany; wróć do konta użytego do skanowania." : "Wylogowano. Skany pozostają na urządzeniu. Zaloguj się, aby wznowić pracę.", Boolean(differentAccount), el("account-message"));
        } catch (error) {
            message("Terminal zablokowany lokalnie. Wylogowanie z serwera czeka na połączenie.", true, el("account-message"));
        } finally { finishingLogout = false; }
    }
    async function logout() {
        if (!db || writes.length || writing || backingUp) throw new Error("Dokończ zapis lub pobieranie kopii przed wylogowaniem.");
        await saveLock({locked: true, logout_pending: true, owner_id: activeAccount || state.owner_id || 0});
        message("Terminal zablokowany. Niewysłane skany zostały zachowane.", false, el("account-message"));
        await finishLogout();
    }

    el("logout-button").addEventListener("click", () => logout().catch(reportError));
    el("resume-button").addEventListener("click", () => loadSessions().then(sync).catch(reportError));
    el("start-button").addEventListener("click", () => start().catch(reportError));
    el("refresh-button").addEventListener("click", () => loadSessions().catch(reportError));
    el("sync-button").addEventListener("click", sync);
    el("backup-button").addEventListener("click", () => backup().catch(reportError));
    el("retry-save").addEventListener("click", () => { if (!writing) void drain(); });
    el("change-session-button").addEventListener("click", () => {
        if (writing || writes.length || backingUp) return message("Najpierw dokończ zapis skanów.", true);
        els.scan.classList.add("terminal-hidden"); els.setup.classList.remove("terminal-hidden");
        void loadSessions().catch(reportError);
    });
    els.input.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        const code = els.input.value.trim();
        if (!code || !state.owner_id || writeFailed || backingUp || localLock.locked || accountMismatch || els.input.readOnly) return;
        writes.push({client_scan_id: id(), code, scanned_at: new Date().toISOString()});
        els.input.value = ""; void drain();
    });
    window.addEventListener("beforeunload", event => {
        if (writes.length) { event.preventDefault(); event.returnValue = ""; }
    });
    window.addEventListener("online", () => { serverReachable = null; connectivity(); void sync(); });
    window.addEventListener("offline", connectivity);
    setInterval(sync, 5000);
    setInterval(storageStatus, 60000);
    (async () => {
        els.input.readOnly = true;
        if (!navigator.locks) throw new Error("Ta przeglądarka nie obsługuje bezpiecznej blokady terminala. Użyj aktualnej przeglądarki Chrome lub Edge.");
        const locked = await new Promise(resolve => {
            navigator.locks.request("lupine-terminal-device", {ifAvailable: true}, lock => {
                resolve(Boolean(lock));
                return lock ? new Promise(() => {}) : undefined;
            }).catch(() => resolve(false));
        });
        if (!locked) throw new Error("Terminal jest już otwarty w innej karcie. Zamknij ją i otwórz tę stronę ponownie.");
        db = await openDB();
        const [savedState, savedLocations, savedLock] = await Promise.all([read("meta", "state"), read("meta", "locations"), read("meta", "lock")]);
        state = (savedState || {}).value || {};
        localLock = (savedLock || {}).value || {};
        lockScreen();
        locationsByCode = (savedLocations || {}).value || {};
        if (!localLock.locked && state.owner_id && state.session_id && state.terminal_id) {
            showScan(); els.input.readOnly = false;
            message("Przywrócono sesję z pamięci urządzenia.");
        }
        await counters(); connectivity();
        void prepareOffline();
        if (localLock.locked) {
            message("Terminal zablokowany. Skany zachowane; zaloguj się, aby wznowić pracę.", false, el("account-message"));
            if (localLock.logout_pending) await finishLogout();
            return;
        }
        if (navigator.onLine) {
            try { await loadSessions(); } catch (error) { reportError(error); }
            void sync();
        } else message(state.owner_id ? "Praca offline. Skany będą wysłane po powrocie internetu." : "Pierwszy start wymaga internetu.");
    })().catch(error => { els.input.readOnly = true; els.start.disabled = true; reportError(error); });
}());
