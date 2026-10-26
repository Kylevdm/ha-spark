import {html, useState, useEffect} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {Axis} from "./axis.js";
import {TodaySpan} from "./today.js";
import {LinesList} from "./lines.js";
import {CarbonStrip} from "./carbon.js";

// Fetch display data once, then share it between the chart and its text keys.
export function PlanParts({health, timeline}) {
    const [selectedLine, selectLine] = useState(null);
    const [history, setHistory] = useState(null);
    const [historyFailed, setHistoryFailed] = useState(false);
    const [carbon, setCarbon] = useState(null);
    const [config, setConfig] = useState(null);
    const available = health.daemon === "running"
        && health.configuration?.status !== "configuration_repair";
    const hasToday = !timeline
        || Date.parse(timeline.horizon_start) > Date.parse(timeline.today_start);
    const to = timeline?.horizon_start;
    useEffect(() => selectLine(null), [timeline]);
    useEffect(() => {
        setHistory(null);
        setHistoryFailed(false);
        if (!available || !hasToday) return;
        let active = true;
        async function load() {
            try {
                const range = to ? `&to=${encodeURIComponent(to)}` : "";
                const response = await fetch(`api/history?resolution=slot${range}`, {cache: "no-store"});
                if (!response.ok) throw new Error();
                const value = await response.json();
                if (active) setHistory(value);
            } catch {
                if (active) setHistoryFailed(true);
            }
        }
        void load();
        return () => { active = false; };
    }, [available, hasToday, to, timeline?.now]);
    useEffect(() => {
        setCarbon(null);
        if (!available || !config || !timeline) return;
        let active = true;
        async function load() {
            try {
                const response = await fetch("api/carbon", {cache: "no-store"});
                if (!response.ok) throw new Error();
                const value = await response.json();
                if (active) setCarbon(value);
            } catch {
                if (active) setCarbon({status: "unavailable", rows: [], attribution: ""});
            }
        }
        void load();
        return () => { active = false; };
    }, [available, config?.settings.carbon_region, config?.revision, timeline?.now]);
    return html`
        ${timeline ? html`<${Axis} timeline=${timeline} selectedLine=${selectedLine}
            history=${hasToday ? history : null} carbon=${carbon}/>` : null}
        <${TodaySpan} payload=${history} failed=${historyFailed} hasToday=${hasToday}/>
        <${CarbonStrip} health=${health} carbon=${carbon} onConfig=${setConfig}/>
        ${timeline ? html`<${LinesList} timeline=${timeline}
            selectedLine=${selectedLine} selectLine=${selectLine}/>` : null}`;
}
