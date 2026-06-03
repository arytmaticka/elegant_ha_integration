/**
 * Elegant Effects Card — multi-zone, multi-effect Lovelace card.
 *
 * Single-zone usage:
 *   type: custom:elegant-effects-card
 *   entity: light.elegant_room_1
 *
 * Multi-zone (whole controller) usage:
 *   type: custom:elegant-effects-card
 *   title: "Living room controller"
 *   entities:
 *     - light.elegant_room_1
 *     - light.elegant_room_2
 *     - light.elegant_room_3
 *   columns: 3
 */

const CARD_VERSION = '1.4.0';

class ElegantEffectsCard extends HTMLElement {

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._selectedZones = new Set();   // zones selected for effect application
  }

  /* ---- Visual editor hookup ---- */

  static getConfigElement() {
    return document.createElement('elegant-effects-card-editor');
  }

  /* ---- Lovelace lifecycle ---- */

  setConfig(config) {
    let entities = [];
    if (Array.isArray(config.entities)) {
      entities = config.entities
        .map(e => (typeof e === 'string' ? e : e?.entity))
        .filter(Boolean);
    } else if (config.entity) {
      entities = [config.entity];
    }
    // Don't throw on empty entities — the card will render a helpful message.
    // Throwing breaks the editor preview and Lovelace card list.

    this._config = { columns: 2, ...config, _entities: entities };
    // By default all zones are selected
    this._selectedZones = new Set(entities);
    this._render();
  }

  set hass(hass) {
    const entities = this._config._entities || [];
    let changed = !this._hass;
    if (!changed) {
      for (const e of entities) {
        if (this._hass.states[e] !== hass.states[e]) {
          changed = true;
          break;
        }
      }
    }
    this._hass = hass;
    if (changed) this._render();
  }

  getCardSize() {
    const entities = this._config._entities || [];
    if (!this._hass || entities.length === 0) return 3;
    let effectCount = 0;
    for (const e of entities) {
      const s = this._hass.states[e];
      if (s?.attributes?.effect_list?.length) {
        effectCount = s.attributes.effect_list.length;
        break;
      }
    }
    const cols = this._config.columns || 2;
    const zonesRows = entities.length > 1 ? 1 : 0;
    return Math.ceil(effectCount / cols) + zonesRows + 2;
  }

  static getStubConfig() {
    return { entities: [], columns: 2 };
  }

  /* ---- Rendering ---- */

  _render() {
    if (!this._hass || !this._config._entities) return;

    const entities = this._config._entities;

    /* No entities configured — friendly message */
    if (entities.length === 0) {
      this.shadowRoot.innerHTML = `
        <ha-card>
          <div style="padding:16px;color:var(--secondary-text-color);font-style:italic">
            ⚙️ Configure the card: pick an Elegant controller and select zones
            in the visual editor.
          </div>
        </ha-card>`;
      return;
    }

    /* Validate entities exist */
    const missing = entities.filter(e => !this._hass.states[e]);
    if (missing.length > 0) {
      this.shadowRoot.innerHTML = `
        <ha-card>
          <div style="padding:16px;color:var(--error-color,#db4437)">
            ⚠️ Entity not found: <code>${this._esc(missing.join(', '))}</code>
          </div>
        </ha-card>`;
      return;
    }

    const multi = entities.length > 1;
    const cols  = this._config.columns || 2;

    const title = this._config.title
      ?? (multi
            ? 'Elegant Effects'
            : (this._hass.states[entities[0]].attributes.friendly_name || entities[0]));

    /* Drop selections referring to entities no longer in config */
    for (const e of [...this._selectedZones]) {
      if (!entities.includes(e)) this._selectedZones.delete(e);
    }
    const selectedEntities = entities.filter(e => this._selectedZones.has(e));

    /* ----------------------------------------------------------------
     * Work by REAL effect IDs (== bit positions in the scenes bitfield).
     * Different controller types can assign different effects/names to
     * the same ID — so we must NEVER share names across types.
     *
     * Display strategy for multi-zone cards:
     *   - pick the "richest" zone (largest effect_names dict) as
     *     the reference for the chip list + labels.
     *   - aggregate state for each ID across ALL selected zones.
     *   - clicking a chip writes the ID to ALL selected zones:
     *     a "poorer" controller will just set a bit it doesn't use,
     *     which is harmless.
     * ---------------------------------------------------------------- */

    const getEffectNames = (ent) => {
      // effect_names is a dict with string keys (JSON serialization).
      const raw = this._hass.states[ent]?.attributes?.effect_names || {};
      return raw;
    };
    const getActiveIds = (ent) => {
      const arr = this._hass.states[ent]?.attributes?.active_effect_ids || [];
      return new Set(arr.map(v => Number(v)));
    };

    // Pick reference zone = the one with the most entries in effect_names
    let refEntity = null;
    let refNames = {};
    for (const e of selectedEntities) {
      const map = getEffectNames(e);
      const count = Object.keys(map).length;
      if (refEntity === null || count > Object.keys(refNames).length) {
        refEntity = e;
        refNames = map;
      }
    }

    // Sorted list of effect entries: [{ id:Number, name:String }]
    const effectList = Object.entries(refNames)
      .map(([id, name]) => ({ id: Number(id), name: String(name) }))
      .filter(e => Number.isFinite(e.id))
      .sort((a, b) => a.id - b.id);

    /* For each effect id → 'all' | 'some' | 'none' across selected zones.
       A zone that doesn't define that id still counts as "not active". */
    const activeState = new Map();
    for (const eff of effectList) {
      let n = 0;
      for (const e of selectedEntities) {
        if (getActiveIds(e).has(eff.id)) n++;
      }
      activeState.set(eff.id,
        n === 0 ? 'none'
        : n === selectedEntities.length ? 'all'
        : 'some');
    }
    const activeCount = [...activeState.values()].filter(v => v !== 'none').length;

    /* Zone chips (only in multi-zone mode) */
    let zonesHtml = '';
    if (multi) {
      let items = '';
      for (const e of entities) {
        const s = this._hass.states[e];
        const name     = s.attributes.friendly_name || e;
        const selected = this._selectedZones.has(e);
        const isOn     = s.state === 'on';
        items += `
          <button type="button"
                  class="zone-chip${selected ? ' selected' : ''}${isOn ? '' : ' off'}"
                  data-entity="${this._esc(e)}">
            <span class="check">${selected ? '✓' : ''}</span>
            <span class="label">${this._esc(name)}</span>
          </button>`;
      }
      zonesHtml = `
        <div class="section-label">Zones (${selectedEntities.length}/${entities.length})</div>
        <div class="zones">${items}</div>
        <div class="zone-actions">
          <button type="button" class="link-btn" id="z-all">All</button>
          <button type="button" class="link-btn" id="z-none">None</button>
        </div>`;
    }

    /* Effects block */
    let effectsHtml = '';
    if (effectList.length === 0) {
      const msg = selectedEntities.length === 0
        ? 'Select at least one zone to configure effects.'
        : 'No effects known for the selected zones.';
      effectsHtml = `<div class="empty">${msg}</div>`;
    } else {
      let chips = '';
      for (const eff of effectList) {
        const state = activeState.get(eff.id);
        const cls   = state === 'all' ? 'active' : (state === 'some' ? 'partial' : '');
        const check = state === 'all' ? '✓' : (state === 'some' ? '–' : '');
        chips += `
          <button type="button" class="chip ${cls}"
                  data-effect-id="${eff.id}"
                  title="ID ${eff.id}">
            <span class="check">${check}</span>
            <span class="label">${this._esc(eff.name)}</span>
          </button>`;
      }
      effectsHtml = `
        ${multi ? '<div class="section-label">Effects</div>' : ''}
        <div class="grid">${chips}</div>
        <div class="actions">
          <button type="button" class="link-btn" id="sel-all">Select all</button>
          <button type="button" class="link-btn" id="sel-none">Clear</button>
        </div>`;
    }

    this.shadowRoot.innerHTML = `
      <style>${this._css(cols)}</style>
      <ha-card>
        <div class="header">
          <span class="title">${this._esc(title)}</span>
          <span class="badge${activeCount === 0 ? ' dim' : ''}">
            ${activeCount}${effectList.length ? ' / ' + effectList.length : ''}
          </span>
        </div>
        ${zonesHtml}
        ${effectsHtml}
      </ha-card>`;

    /* Event listeners */
    this.shadowRoot.querySelectorAll('.zone-chip').forEach(chip =>
      chip.addEventListener('click', () => this._toggleZone(chip.dataset.entity))
    );
    this.shadowRoot.getElementById('z-all')
      ?.addEventListener('click', () => {
        this._selectedZones = new Set(entities);
        this._render();
      });
    this.shadowRoot.getElementById('z-none')
      ?.addEventListener('click', () => {
        this._selectedZones = new Set();
        this._render();
      });

    const allIds = effectList.map(e => e.id);
    this.shadowRoot.querySelectorAll('.chip').forEach(chip =>
      chip.addEventListener('click', () =>
        this._onToggleEffectId(Number(chip.dataset.effectId))
      )
    );
    this.shadowRoot.getElementById('sel-all')
      ?.addEventListener('click', () => this._applyIdsToAll(allIds));
    this.shadowRoot.getElementById('sel-none')
      ?.addEventListener('click', () => this._applyIdsToAll([]));
  }

  /* ---- Actions ---- */

  _toggleZone(entityId) {
    if (this._selectedZones.has(entityId)) this._selectedZones.delete(entityId);
    else                                    this._selectedZones.add(entityId);
    this._render();
  }

  /**
   * Toggle an effect by its real ID (== bit position in scenes bitfield).
   * If the effect is active in ALL selected zones → remove from all.
   * Otherwise → add to all selected zones.
   * Unsupported bits on zones that don't define this ID are harmless.
   */
  _onToggleEffectId(effectId) {
    const selected = (this._config._entities || []).filter(e => this._selectedZones.has(e));
    if (selected.length === 0 || !Number.isFinite(effectId)) return;

    let activeInAll = true;
    for (const e of selected) {
      const ids = new Set(
        (this._hass.states[e]?.attributes?.active_effect_ids || [])
          .map(v => Number(v))
      );
      if (!ids.has(effectId)) { activeInAll = false; break; }
    }

    for (const e of selected) {
      const current = new Set(
        (this._hass.states[e]?.attributes?.active_effect_ids || [])
          .map(v => Number(v))
      );
      if (activeInAll) current.delete(effectId);
      else             current.add(effectId);
      this._callServiceByIds(e, [...current]);
    }
  }

  /**
   * Apply an explicit list of effect IDs to every selected zone.
   * Used by "Select all" and "Clear".
   */
  _applyIdsToAll(effectIds) {
    const selected = (this._config._entities || []).filter(e => this._selectedZones.has(e));
    for (const e of selected) {
      this._callServiceByIds(e, effectIds);
    }
  }

  _callServiceByIds(entityId, effectIds) {
    const stateObj  = this._hass.states[entityId];
    const zoneIndex = stateObj?.attributes?.zone_index;
    if (zoneIndex === undefined || zoneIndex === null) {
      console.error('[elegant-effects-card] zone_index missing for', entityId);
      return;
    }
    this._hass.callService('elegant', 'set_zone_effects', {
      zone_index: zoneIndex,
      effect_ids: effectIds.map(v => Number(v)).filter(Number.isFinite),
    });
  }

  /* ---- Helpers ---- */

  _esc(s) {
    const d = document.createElement('span');
    d.textContent = String(s);
    return d.innerHTML;
  }

  _css(cols) {
    return `
      :host {
        --chip-bg:          var(--card-background-color, #fff);
        --chip-active-bg:   var(--primary-color, #03a9f4);
        --chip-active-text: var(--text-primary-color, #fff);
        --chip-border:      var(--divider-color, #e0e0e0);
        --chip-text:        var(--primary-text-color, #212121);
        --chip-hover:       var(--secondary-background-color, #f5f5f5);
      }

      ha-card { overflow: hidden; }

      /* ---- header ---- */
      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 16px 16px 4px;
      }
      .title {
        font-size: 1.1rem;
        font-weight: 500;
        color: var(--primary-text-color);
      }
      .badge {
        font-size: 0.75rem;
        font-weight: 600;
        padding: 2px 10px;
        border-radius: 12px;
        background: var(--chip-active-bg);
        color: var(--chip-active-text);
        transition: opacity .2s;
        white-space: nowrap;
      }
      .badge.dim { opacity: 0.3; }

      /* ---- section label ---- */
      .section-label {
        padding: 10px 16px 4px;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--secondary-text-color);
      }

      /* ---- zones row ---- */
      .zones {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        padding: 4px 16px 4px;
      }
      .zone-actions {
        display: flex;
        gap: 4px;
        padding: 0 16px 4px;
        justify-content: flex-end;
      }
      .zone-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 10px;
        border-radius: 16px;
        border: 1.5px solid var(--chip-border);
        background: var(--chip-bg);
        color: var(--chip-text);
        cursor: pointer;
        font-size: 0.8rem;
        font-family: inherit;
        user-select: none;
        -webkit-user-select: none;
        transition: all .15s ease;
      }
      .zone-chip:hover {
        background: var(--chip-hover);
        border-color: var(--chip-active-bg);
      }
      .zone-chip.selected {
        background: var(--chip-active-bg);
        border-color: var(--chip-active-bg);
        color: var(--chip-active-text);
      }
      .zone-chip.selected:hover { opacity: 0.88; }
      .zone-chip.off { opacity: 0.55; font-style: italic; }
      .zone-chip .check {
        width: 16px; height: 16px;
        border-radius: 4px;
        border: 2px solid var(--chip-border);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 11px;
        font-weight: 700;
        flex-shrink: 0;
      }
      .zone-chip.selected .check {
        background: var(--chip-active-text);
        border-color: var(--chip-active-text);
        color: var(--chip-active-bg);
      }

      /* ---- effect grid ---- */
      .grid {
        display: grid;
        grid-template-columns: repeat(${cols}, 1fr);
        gap: 8px;
        padding: 8px 16px;
      }

      /* ---- effect chip ---- */
      .chip {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1.5px solid var(--chip-border);
        background: var(--chip-bg);
        color: var(--chip-text);
        cursor: pointer;
        font-size: 0.875rem;
        font-family: inherit;
        user-select: none;
        -webkit-user-select: none;
        transition: all .15s ease;
        text-align: left;
      }
      .chip:hover {
        background: var(--chip-hover);
        border-color: var(--chip-active-bg);
      }
      .chip.active {
        background: var(--chip-active-bg);
        border-color: var(--chip-active-bg);
        color: var(--chip-active-text);
      }
      .chip.active:hover { opacity: 0.88; }

      /* partial: effect active in some (but not all) selected zones */
      .chip.partial {
        background: var(--chip-bg);
        border-color: var(--chip-active-bg);
        border-style: dashed;
        color: var(--chip-active-bg);
      }
      .chip.partial .check {
        background: var(--chip-active-bg);
        border-color: var(--chip-active-bg);
        color: var(--chip-active-text);
      }

      .chip .check {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        width: 20px;
        height: 20px;
        border-radius: 5px;
        border: 2px solid var(--chip-border);
        font-size: 13px;
        font-weight: 700;
        transition: all .15s ease;
      }
      .chip.active .check {
        background: var(--chip-active-text);
        border-color: var(--chip-active-text);
        color: var(--chip-active-bg);
      }

      .label {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      /* ---- action buttons ---- */
      .actions {
        display: flex;
        gap: 8px;
        padding: 4px 16px 14px;
        justify-content: flex-end;
      }
      .link-btn {
        background: none;
        border: none;
        color: var(--primary-color);
        cursor: pointer;
        font-size: 0.8rem;
        font-family: inherit;
        padding: 4px 10px;
        border-radius: 6px;
        transition: background .15s;
      }
      .link-btn:hover { background: var(--chip-hover); }

      /* ---- empty state ---- */
      .empty {
        padding: 16px;
        color: var(--secondary-text-color);
        font-style: italic;
        text-align: center;
      }
    `;
  }
}

/* ---- Register ---- */

/* ============================================================
 * Visual configuration editor
 * ============================================================ */

class ElegantEffectsCardEditor extends HTMLElement {

  constructor() {
    super();
    this._config = {};
    this._hass = null;
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._render();
  }

  /* ---- Helpers ---- */

  _getDeviceEntities(deviceId) {
    if (!this._hass || !deviceId) return [];
    const entities = this._hass.entities || {};
    return Object.values(entities)
      .filter(e => e.device_id === deviceId && e.entity_id.startsWith('light.'))
      .map(e => e.entity_id)
      .sort();
  }

  _fireChange(newConfig) {
    this._config = newConfig;
    this.dispatchEvent(new CustomEvent('config-changed', {
      detail: { config: newConfig },
      bubbles: true,
      composed: true,
    }));
    this._render();
  }

  /* ---- Change handlers ---- */

  _onTitleChange(value) {
    const c = { ...this._config };
    if (value) c.title = value;
    else       delete c.title;
    this._fireChange(c);
  }

  _onDeviceChanged(deviceId) {
    const entityList = this._getDeviceEntities(deviceId);
    const c = { ...this._config, entities: entityList };
    if (deviceId) c.device_id = deviceId;
    else          delete c.device_id;
    delete c.entity;  // clean up legacy single-entity field
    this._fireChange(c);
  }

  _onEntityToggle(entityId, checked) {
    const current = new Set(this._config.entities || []);
    if (checked) current.add(entityId);
    else         current.delete(entityId);
    const c = { ...this._config, entities: [...current] };
    delete c.entity;
    this._fireChange(c);
  }

  _onColumnsChange(value) {
    const n = Number(value) || 2;
    this._fireChange({ ...this._config, columns: n });
  }

  /* ---- Render ---- */

  _render() {
    try {
      this._renderInner();
    } catch (err) {
      console.error('[elegant-effects-card-editor] render failed:', err);
      this.innerHTML = `
        <div style="padding:12px;color:var(--error-color,#db4437);">
          Editor render failed: ${String(err?.message || err)}.
          Check browser console for details.
        </div>`;
    }
  }

  _renderInner() {
    if (!this._hass) return;

    const deviceId = this._config.device_id || '';
    const deviceEntities = this._getDeviceEntities(deviceId);

    // Compute current selection (supporting legacy `entity:`)
    const selected = new Set(
      this._config.entities
        ?? (this._config.entity ? [this._config.entity] : [])
    );

    this.innerHTML = '';

    const style = document.createElement('style');
    style.textContent = `
      .editor { display: block; padding: 8px 0; }
      ha-form { display: block; }
      .section-title {
        font-size: 0.75rem;
        font-weight: 600;
        color: var(--secondary-text-color);
        padding: 14px 0 2px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .entity-list {
        display: flex;
        flex-direction: column;
        gap: 0;
        padding: 4px 0;
      }
      .entity-row {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 4px;
        cursor: pointer;
        border-radius: 6px;
      }
      .entity-row:hover { background: var(--secondary-background-color); }
      .entity-row input[type="checkbox"] {
        width: 18px;
        height: 18px;
        accent-color: var(--primary-color);
        cursor: pointer;
        flex-shrink: 0;
      }
      .entity-row .name {
        font-size: 0.9rem;
        color: var(--primary-text-color);
      }
      .entity-row .eid {
        font-size: 0.75rem;
        color: var(--secondary-text-color);
        margin-left: 4px;
      }
      .actions {
        display: flex;
        gap: 4px;
        justify-content: flex-end;
        padding: 2px 0 8px;
      }
      .link-btn {
        background: none;
        border: none;
        color: var(--primary-color);
        cursor: pointer;
        font-size: 0.8rem;
        padding: 4px 10px;
        border-radius: 6px;
      }
      .link-btn:hover { background: var(--secondary-background-color); }
      .hint {
        color: var(--secondary-text-color);
        font-style: italic;
        padding: 8px 0;
        font-size: 0.85rem;
      }
    `;
    this.appendChild(style);

    const root = document.createElement('div');
    root.className = 'editor';

    /* --- Main form (ha-form handles loading of its own sub-elements) --- */
    const form = document.createElement('ha-form');
    form.hass = this._hass;
    form.data = {
      title: this._config.title || '',
      device_id: deviceId,
      columns: this._config.columns || 2,
    };
    form.schema = [
      { name: 'title', selector: { text: {} } },
      {
        name: 'device_id',
        selector: {
          device: {
            // Filter to devices belonging to the 'elegant' integration.
            // Falls back gracefully if the filter is not supported.
            filter: { integration: 'elegant' },
          },
        },
      },
      {
        name: 'columns',
        selector: { number: { min: 1, max: 4, mode: 'box' } },
      },
    ];
    form.computeLabel = (field) => ({
      title: 'Title (optional)',
      device_id: 'Elegant controller',
      columns: 'Columns',
    }[field.name] || field.name);
    form.addEventListener('value-changed', (ev) =>
      this._onFormChange(ev.detail?.value || {})
    );
    root.appendChild(form);

    /* --- Entity checklist (built manually with native checkboxes) --- */
    if (deviceId) {
      if (deviceEntities.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'hint';
        empty.textContent = 'No light entities found for this device.';
        root.appendChild(empty);
      } else {
        const activeCount =
          [...selected].filter(e => deviceEntities.includes(e)).length;

        const sTitle = document.createElement('div');
        sTitle.className = 'section-title';
        sTitle.textContent = `Zones (${activeCount}/${deviceEntities.length})`;
        root.appendChild(sTitle);

        const list = document.createElement('div');
        list.className = 'entity-list';
        for (const ent of deviceEntities) {
          const state = this._hass.states[ent];
          const friendly = state?.attributes?.friendly_name || ent;

          const row = document.createElement('label');
          row.className = 'entity-row';

          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = selected.has(ent);
          cb.addEventListener('change', (ev) =>
            this._onEntityToggle(ent, ev.target.checked)
          );

          const nameSpan = document.createElement('span');
          nameSpan.className = 'name';
          nameSpan.textContent = friendly;

          const eidSpan = document.createElement('span');
          eidSpan.className = 'eid';
          eidSpan.textContent = `(${ent})`;

          row.appendChild(cb);
          row.appendChild(nameSpan);
          row.appendChild(eidSpan);
          list.appendChild(row);
        }
        root.appendChild(list);

        const actions = document.createElement('div');
        actions.className = 'actions';

        const selAll = document.createElement('button');
        selAll.type = 'button';
        selAll.className = 'link-btn';
        selAll.textContent = 'Select all';
        selAll.addEventListener('click', () => {
          const c = { ...this._config, entities: [...deviceEntities] };
          delete c.entity;
          this._fireChange(c);
        });

        const selNone = document.createElement('button');
        selNone.type = 'button';
        selNone.className = 'link-btn';
        selNone.textContent = 'Clear';
        selNone.addEventListener('click', () => {
          const c = { ...this._config, entities: [] };
          delete c.entity;
          this._fireChange(c);
        });

        actions.appendChild(selAll);
        actions.appendChild(selNone);
        root.appendChild(actions);
      }
    } else {
      const hint = document.createElement('div');
      hint.className = 'hint';
      hint.textContent =
        'Pick an Elegant controller above to choose which zones the card controls.';
      root.appendChild(hint);
    }

    this.appendChild(root);
  }

  /* ---- Form handler (combines title, device_id, columns changes) ---- */
  _onFormChange(values) {
    const prev = this._config;
    const c = { ...prev };

    // Title
    if (values.title) c.title = values.title;
    else              delete c.title;

    // Columns
    c.columns = Number(values.columns) || 2;

    // Device change → auto-fill entities with all lights of that device
    const newDeviceId = values.device_id || '';
    const oldDeviceId = prev.device_id || '';
    if (newDeviceId !== oldDeviceId) {
      if (newDeviceId) {
        c.device_id = newDeviceId;
        c.entities = this._getDeviceEntities(newDeviceId);
      } else {
        delete c.device_id;
        c.entities = [];
      }
      delete c.entity;
    }

    this._fireChange(c);
  }
}

/* ============================================================
 * Register elements (each isolated so one failure doesn't
 * prevent the other from registering)
 * ============================================================ */

function _safeDefine(tagName, klass) {
  try {
    if (customElements.get(tagName)) {
      console.info(`[elegant-effects-card] ${tagName} already registered`);
      return;
    }
    customElements.define(tagName, klass);
  } catch (err) {
    console.error(`[elegant-effects-card] failed to register <${tagName}>:`, err);
  }
}

_safeDefine('elegant-effects-card-editor', ElegantEffectsCardEditor);
_safeDefine('elegant-effects-card', ElegantEffectsCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'elegant-effects-card',
  name: 'Elegant Effects Card',
  description: 'Multi-zone, multi-effect control for Elegant LED Controller',
  preview: true,
});

console.info(
  `%c ELEGANT-EFFECTS-CARD %c v${CARD_VERSION} `,
  'color:#fff; background:#03a9f4; font-weight:700; padding:2px 6px; border-radius:4px 0 0 4px',
  'color:#03a9f4; background:#e3f2fd; font-weight:700; padding:2px 6px; border-radius:0 4px 4px 0',
);

