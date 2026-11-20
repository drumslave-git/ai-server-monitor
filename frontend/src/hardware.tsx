// The hardware meters at the foot of the rail on every page: CPU, RAM and per NVIDIA GPU its use
// and VRAM, read now. Asked for every 5 s while the tab is visible; not while it is hidden.
import { useEffect, useState } from "react";
import { api, type Schemas } from "./api";
import { Gauge } from "./kit";

type Hardware = Schemas["HardwareResponse"];

const EVERY_MS = 5_000;
const GIB = 1024 ** 3;

/** The newest reading; null before the first answer and after a failed request. The next request
 *  goes out 5 s after the previous answer, so a slow answer never has a second request beside it. */
function useHardware(): Hardware | null {
  const [data, setData] = useState<Hardware | null>(null);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    let asking = false;
    const ask = () => {
      clearTimeout(timer);
      if (asking || document.visibilityState !== "visible") return;
      asking = true;
      api.get<Hardware>("/api/hardware").then(
        (d) => alive && setData(d),
        () => alive && setData(null),
      ).finally(() => {
        asking = false;
        if (alive && document.visibilityState === "visible") timer = setTimeout(ask, EVERY_MS);
      });
    };
    const onVisibility = () => (document.visibilityState === "visible" ? ask() : clearTimeout(timer));
    ask();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      alive = false;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);
  return data;
}

const percent = (v: number) => `${Math.round(v)}%`;
const gib = (used: number, total: number) => `${(used / GIB).toFixed(1)}/${(total / GIB).toFixed(1)} GiB`;

export function HardwareMeters() {
  const hw = useHardware();
  if (!hw) return null;
  const several = hw.gpus.length > 1;
  return (
    <>
      <Gauge label="CPU" value={hw.cpu_percent} max={100} text={percent(hw.cpu_percent)} />
      <Gauge label="RAM" value={hw.ram_used} max={hw.ram_total} text={gib(hw.ram_used, hw.ram_total)} />
      {hw.gpus.map((gpu, i) => {
        const n = several ? ` ${i}` : "";
        return [
          <Gauge key={`gpu${i}`} label={`GPU${n}`} value={gpu.util_percent} max={100} text={percent(gpu.util_percent)} title={gpu.name} />,
          <Gauge key={`vram${i}`} label={`VRAM${n}`} value={gpu.vram_used} max={gpu.vram_total} text={gib(gpu.vram_used, gpu.vram_total)} title={gpu.name} />,
        ];
      })}
    </>
  );
}
