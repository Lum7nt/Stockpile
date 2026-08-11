const fmtISK = (value) =>
  new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value || 0));

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

window.ledgerApp = function ledgerApp() {
  return {
    activeTab: "dashboard",
    busy: false,
    message: "",
    accountMenuOpen: false,
    lastSyncedAt: localStorage.getItem("eve_last_synced_at") || "",
    selectedCharacterId: "all",
    orderSearch: "",
    orderSort: { key: "total_value", direction: "desc" },
    settings: {
      client_id: "",
      callback_url: "http://localhost:8000/auth/callback",
      default_low_stock_percent: 20,
      required_scopes: [],
      characters: [],
      client_secret_configured: false,
    },
    dashboard: {
      totals: { wallet_balance: 0, active_order_value: 0, asset_estimate: 0, restock_alerts: 0 },
      characters: [],
      known_types: 0,
    },
    orders: [],
    restock: [],
    thresholds: [],
    thresholdDrafts: {},
    toast: {
      show: false,
      text: "",
      tone: "info",
    },

    async init() {
      await this.reloadAll();
    },

    async reloadAll() {
      this.busy = true;
      try {
        const [settingsResult, dashboardResult, ordersResult, restockResult, thresholdsResult] = await Promise.allSettled([
          fetch("/api/settings").then(async (r) => {
            if (!r.ok) throw new Error("/api/settings failed");
            return r.json();
          }),
          fetch("/api/dashboard").then(async (r) => {
            if (!r.ok) throw new Error("/api/dashboard failed");
            return r.json();
          }),
          fetch("/api/orders").then(async (r) => {
            if (!r.ok) throw new Error("/api/orders failed");
            return r.json();
          }),
          fetch("/api/restock").then(async (r) => {
            if (!r.ok) throw new Error("/api/restock failed");
            return r.json();
          }),
          fetch("/api/thresholds").then(async (r) => {
            if (!r.ok) throw new Error("/api/thresholds failed");
            return r.json();
          }),
        ]);

        if (settingsResult.status === "fulfilled") {
          this.settings = { ...this.settings, ...settingsResult.value };
        }

        if (dashboardResult.status === "fulfilled") {
          this.dashboard = {
            totals: {
              wallet_balance: 0,
              active_order_value: 0,
              asset_estimate: 0,
              restock_alerts: 0,
              ...(dashboardResult.value.totals || {}),
            },
            characters: dashboardResult.value.characters || [],
            known_types: dashboardResult.value.known_types || 0,
          };
        }

        if (ordersResult.status === "fulfilled") {
          this.orders = (ordersResult.value.items || []).map((item) => ({
            ...item,
            item_name: item.item_name || item.type_name || `Type ${item.type_id}`,
            expires_at: item.expires_at || item.issued || "",
          }));
        }

        if (restockResult.status === "fulfilled") {
          this.restock = restockResult.value.items || [];
        }

        if (thresholdsResult.status === "fulfilled") {
          this.thresholds = thresholdsResult.value.items || [];
        }

        const failed = [settingsResult, dashboardResult, ordersResult, restockResult, thresholdsResult].filter(
          (result) => result.status === "rejected",
        );
        if (failed.length) {
          this.showToast("Some dashboard data could not be loaded. Showing available data.", "error");
        }
      } catch (error) {
        this.showToast(error.message || "Failed to load dashboard data.", "error");
      } finally {
        this.busy = false;
      }
    },

    async saveSettings() {
      this.busy = true;
      try {
        const res = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            client_id: this.settings.client_id,
            client_secret: null,
            callback_url: this.settings.callback_url,
            default_low_stock_percent: Number(this.settings.default_low_stock_percent),
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(data.detail || "Could not save application settings.");
        }
        await this.reloadAll();
        this.showToast("Application settings saved.", "success");
      } catch (error) {
        this.showToast(error.message || "Could not save application settings.", "error");
      } finally {
        this.busy = false;
      }
    },

    startLogin() {
      window.location.href = "/auth/login";
    },

    async sync(characterId = null) {
      this.busy = true;
      try {
        const effectiveCharacterId =
          characterId ?? (this.selectedCharacterId === "all" ? null : Number(this.selectedCharacterId));
        const res = await fetch("/api/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ character_id: effectiveCharacterId }),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Sync failed");
        }
        this.lastSyncedAt = new Date().toISOString();
        localStorage.setItem("eve_last_synced_at", this.lastSyncedAt);
        await this.reloadAll();
        this.showToast("ESI sync complete.", "success");
      } catch (error) {
        this.showToast(error.message || "Sync failed.", "error");
      } finally {
        this.busy = false;
      }
    },

    showToast(text, tone = "info") {
      this.message = text;
      this.toast = { show: true, text, tone };
      window.clearTimeout(this._toastTimer);
      this._toastTimer = window.setTimeout(() => {
        this.toast.show = false;
      }, 3200);
    },

    selectCharacter(id) {
      this.selectedCharacterId = id;
      this.accountMenuOpen = false;
    },

    selectedCharacterMeta() {
      const linkedCount = this.linkedCharacterCount();
      if (this.selectedCharacterId === "all") {
        return {
          label: "All Characters",
          subtitle: `${linkedCount} linked capsuleers`,
          avatar: "",
          total: this.filteredStats().walletOnly + this.filteredStats().activeOrderValue,
        };
      }
      const character = this.dashboard.characters.find(
        (item) => String(item.character_id) === String(this.selectedCharacterId),
      );
      if (!character) {
        return {
          label: "All Characters",
          subtitle: `${linkedCount} linked capsuleers`,
          avatar: "",
          total: this.filteredStats().walletOnly + this.filteredStats().activeOrderValue,
        };
      }
      return {
        label: character.character_name,
        subtitle: `${character.active_sell_orders} active sell orders`,
        avatar: character.avatar_url,
        total: (character.wallet_balance || 0) + (character.active_order_value || 0),
      };
    },

    characterOptions() {
      const allOption = {
        character_id: "all",
        character_name: "All Characters",
        avatar_url: "",
        total_isk: this.filteredStats().walletOnly + this.filteredStats().activeOrderValue,
      };
      const dashboardById = new Map(
        this.dashboard.characters.map((character) => [String(character.character_id), character]),
      );
      const baseCharacters =
        this.settings.characters.length > 0
          ? this.settings.characters
          : this.dashboard.characters;
      const chars = baseCharacters.map((character) => {
        const enriched = dashboardById.get(String(character.character_id)) || {};
        return {
          ...character,
          ...enriched,
          total_isk: Number(enriched.wallet_balance || 0) + Number(enriched.active_order_value || 0),
        };
      });
      return [allOption, ...chars];
    },

    linkedCharacterCount() {
      return this.settings.characters.length || this.dashboard.characters.length || 0;
    },

    filteredCharacters() {
      if (this.selectedCharacterId === "all") {
        return this.dashboard.characters;
      }
      return this.dashboard.characters.filter(
        (character) => String(character.character_id) === String(this.selectedCharacterId),
      );
    },

    filteredOrders() {
      const search = this.orderSearch.trim().toLowerCase();
      const filtered = this.orders.filter((item) => {
        const matchesCharacter =
          this.selectedCharacterId === "all" || String(item.character_id) === String(this.selectedCharacterId);
        const matchesSearch =
          !search ||
          (item.item_name || "").toLowerCase().includes(search) ||
          (item.location_name || "").toLowerCase().includes(search) ||
          (item.character_name || "").toLowerCase().includes(search) ||
          String(item.type_id).includes(search);
        return matchesCharacter && matchesSearch;
      });

      const sorted = [...filtered];
      const direction = this.orderSort.direction === "asc" ? 1 : -1;
      sorted.sort((a, b) => {
        const key = this.orderSort.key;
        const valueA = this.orderSortValue(a, key);
        const valueB = this.orderSortValue(b, key);
        if (valueA < valueB) return -1 * direction;
        if (valueA > valueB) return 1 * direction;
        return 0;
      });
      return sorted;
    },

    orderSortValue(item, key) {
      if (key === "item_name" || key === "location_name" || key === "character_name") {
        return String(item[key] || "").toLowerCase();
      }
      if (key === "expires_at") {
        return new Date(item.expires_at || 0).getTime();
      }
      return Number(item[key] || 0);
    },

    sortOrders(key) {
      if (this.orderSort.key === key) {
        this.orderSort.direction = this.orderSort.direction === "asc" ? "desc" : "asc";
      } else {
        this.orderSort = {
          key,
          direction: ["item_name", "location_name", "character_name", "expires_at"].includes(key) ? "asc" : "desc",
        };
      }
    },

    filteredStats() {
      const characters = this.filteredCharacters();
      const walletOnly = characters.reduce((sum, character) => sum + Number(character.wallet_balance || 0), 0);
      const activeOrderValue = characters.reduce(
        (sum, character) => sum + Number(character.active_order_value || 0),
        0,
      );
      const activeOrderCount = this.filteredOrders().length;
      const lowStockWarnings = this.restockRows().filter((row) => row.status !== "healthy").length;
      return {
        walletOnly,
        activeOrderValue,
        totalCombined: walletOnly + activeOrderValue,
        activeOrderCount,
        lowStockWarnings,
      };
    },

    restockRows() {
      const restockMap = new Map(
        this.restock.map((item) => [`${item.character_id}:${item.type_id}`, item]),
      );

      return this.filteredOrders()
        .map((order) => {
          const threshold = this.thresholdFor(order.character_id, order.type_id);
          const thresholdQty = threshold?.min_quantity ?? 0;
          const thresholdPercent =
            threshold?.low_stock_percent ?? Number(this.settings.default_low_stock_percent || 20);
          const remainPercent =
            order.remaining_percent ??
            (order.volume_total ? (Number(order.volume_remain) / Number(order.volume_total)) * 100 : 0);
          const linked = restockMap.get(`${order.character_id}:${order.type_id}`);
          const stockOnHand = linked?.stock_on_hand ?? 0;
          const requiredRestockQty =
            linked?.required_restock_qty ?? Math.max(Math.max(order.volume_total, thresholdQty) - stockOnHand, 0);
          const quantityRemaining = linked?.quantity_remaining ?? order.volume_remain;

          let status = "healthy";
          if (Number(quantityRemaining) <= 0) {
            status = "critical";
          } else if (Number(quantityRemaining) <= thresholdQty || Number(remainPercent) <= thresholdPercent) {
            status = "low";
          }

          return {
            ...order,
            stock_on_hand: stockOnHand,
            required_restock_qty: requiredRestockQty,
            daily_velocity: linked?.daily_velocity ?? 0,
            quantity_remaining: quantityRemaining,
            remaining_percent: Number(remainPercent),
            threshold_min_quantity: thresholdQty,
            threshold_low_stock_percent: thresholdPercent,
            status,
          };
        })
        .sort((a, b) => {
          const orderRank = { critical: 0, low: 1, healthy: 2 };
          return (
            orderRank[a.status] - orderRank[b.status] ||
            a.remaining_percent - b.remaining_percent ||
            b.total_value - a.total_value
          );
        });
    },

    thresholdFor(characterId, typeId) {
      return (
        this.thresholds.find(
          (rule) => String(rule.character_id) === String(characterId) && String(rule.type_id) === String(typeId),
        ) ||
        this.thresholds.find((rule) => rule.character_id == null && String(rule.type_id) === String(typeId)) ||
        null
      );
    },

    thresholdDraftKey(item) {
      return `${item.character_id}:${item.type_id}`;
    },

    thresholdDraft(item) {
      const key = this.thresholdDraftKey(item);
      if (!this.thresholdDrafts[key]) {
        const threshold = this.thresholdFor(item.character_id, item.type_id);
        this.thresholdDrafts[key] = {
          character_id: item.character_id,
          type_id: item.type_id,
          min_quantity: threshold?.min_quantity ?? 0,
          low_stock_percent: threshold?.low_stock_percent ?? this.settings.default_low_stock_percent,
          saving: false,
        };
      }
      return this.thresholdDrafts[key];
    },

    async saveThresholdForRow(item) {
      const draft = this.thresholdDraft(item);
      draft.saving = true;
      try {
        const res = await fetch("/api/thresholds", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            character_id: Number(draft.character_id),
            type_id: Number(draft.type_id),
            min_quantity: Number(draft.min_quantity),
            low_stock_percent: draft.low_stock_percent === "" ? null : Number(draft.low_stock_percent),
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(data.detail || "Threshold save failed");
        }
        await this.reloadAll();
        this.showToast(`Threshold updated for ${item.item_name}.`, "success");
      } catch (error) {
        this.showToast(error.message || "Threshold save failed.", "error");
      } finally {
        draft.saving = false;
      }
    },

    statusBadge(status) {
      if (status === "critical") {
        return {
          label: "Critical",
          classes: "border-rose-500/40 bg-rose-500/15 text-rose-300 shadow-[0_0_20px_rgba(244,63,94,0.18)]",
        };
      }
      if (status === "low") {
        return {
          label: "Low Stock",
          classes: "border-amber-500/40 bg-amber-500/15 text-amber-300 shadow-[0_0_20px_rgba(251,191,36,0.18)]",
        };
      }
      return {
        label: "Healthy",
        classes: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300 shadow-[0_0_20px_rgba(16,185,129,0.18)]",
      };
    },

    formatLastSynced() {
      if (!this.lastSyncedAt) {
        return "No sync yet";
      }
      const diffSeconds = Math.round((new Date(this.lastSyncedAt).getTime() - Date.now()) / 1000);
      const abs = Math.abs(diffSeconds);
      if (abs < 60) return rtf.format(Math.round(diffSeconds), "second");
      if (abs < 3600) return rtf.format(Math.round(diffSeconds / 60), "minute");
      if (abs < 86400) return rtf.format(Math.round(diffSeconds / 3600), "hour");
      return rtf.format(Math.round(diffSeconds / 86400), "day");
    },

    formatExpiry(order) {
      if (!order.expires_at) return "Unknown";
      const expiresAt = new Date(order.expires_at);
      const diffSeconds = Math.round((expiresAt.getTime() - Date.now()) / 1000);
      if (diffSeconds <= 0) return "Expired";
      const abs = Math.abs(diffSeconds);
      if (abs < 3600) return rtf.format(Math.round(diffSeconds / 60), "minute");
      if (abs < 86400) return rtf.format(Math.round(diffSeconds / 3600), "hour");
      return rtf.format(Math.round(diffSeconds / 86400), "day");
    },

    fmtISK,
  };
};
