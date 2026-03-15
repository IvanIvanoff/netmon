# netmon Statistics Guide

A complete guide to every metric netmon collects, what it means, why it matters for video calls, and what values indicate problems.

---

## How netmon works

netmon samples your network every 2 seconds. Each sample probes several layers of your connection simultaneously:

```
Your Mac  →  WiFi radio  →  Router (gateway)  →  ISP  →  Internet (8.8.8.8)
```

Problems at any layer cause different symptoms. netmon measures each layer independently so you can pinpoint exactly where things break down.

---

## 1. Basic Network Info

These identify your current connection. They don't directly measure quality, but changes in these values often coincide with problems.

### SSID

**What:** The name of the WiFi network you're connected to.

**Why it matters:** If this changes mid-call, your Mac switched networks — probably causing a brief disconnection. This can happen when your Mac decides a different network has better signal, or when you move between floors.

### Interface

**What:** The network interface being used (usually `en0` for WiFi on Macs).

**Why it matters:** Purely informational. If this changes, your Mac switched from WiFi to Ethernet or vice versa.

### Local IP / Public IP

**What:** Your IP address on the local network (e.g. `192.168.1.42`) and your public-facing IP on the internet.

**Why it matters:** If your local IP changes, your router reassigned your address (DHCP lease renewal) — this can briefly interrupt connections. If your public IP changes, your ISP rotated your address, which can break long-lived connections like video calls.

### Gateway IP

**What:** Your router's IP address on the local network (e.g. `192.168.1.1`).

**Why it matters:** This is the first hop your traffic takes. By measuring latency to the gateway separately from the internet, netmon can tell whether problems are on your local network or further upstream.

### BSSID

**What:** The MAC address of the specific WiFi access point you're connected to.

**Why it matters:** If you have multiple access points (mesh network, WiFi extenders), your Mac may roam between them. Each roam causes a brief disconnection (typically 50-200ms). Frequent roaming during a call means your signal is marginal from all access points — you're in a dead zone.

---

## 2. WiFi Signal Quality

These measure the radio link between your Mac and the access point. This is the most common source of video call problems.

### RSSI (dBm) — Received Signal Strength Indicator

**What:** How strong the WiFi signal is at your Mac's antenna. Measured in decibels relative to a milliwatt (dBm). Values are always negative — closer to zero is stronger.

**Scale:**
| Value | Quality | What to expect |
|-------|---------|----------------|
| -30 to -50 | Excellent | Right next to the router |
| -50 to -60 | Good | Same room, reliable for video |
| -60 to -67 | Fair | May work for video, occasional issues |
| -67 to -75 | Weak | Video calls will stutter and freeze |
| Below -75 | Very weak | Unusable for real-time communication |

**Why it causes problems:** Weaker signal means your WiFi adapter must use slower, more error-resistant encoding (lower MCS index). This reduces throughput and increases the chance of dropped packets. At very low signal, the adapter may repeatedly fail to send frames, causing visible lag.

**How to fix:** Move closer to the router, or add an access point closer to where you take calls.

### Noise (dBm) — Background Noise Floor

**What:** The amount of radio interference on your WiFi channel. Also in dBm. Typical values are -85 to -100 dBm.

**Why it causes problems:** Other devices broadcasting on the same frequency add noise: microwaves (2.4 GHz), Bluetooth devices, neighboring WiFi networks, baby monitors, and some USB 3.0 devices. High noise reduces the effective signal quality even if RSSI is good. Think of it as trying to have a conversation in a loud room — even if the speaker is close, you can't hear them over the noise.

**What's bad:** Noise above -80 dBm is concerning. Above -70 dBm is very noisy.

### SNR (dB) — Signal-to-Noise Ratio

**What:** The gap between your signal strength and the noise floor. Calculated as `RSSI - Noise`. This is the single most important WiFi metric because it captures both signal and interference in one number.

**Scale:**
| Value | Quality | Meaning |
|-------|---------|---------|
| Above 40 dB | Excellent | Clean, strong connection |
| 25-40 dB | Good | Reliable for all uses |
| 20-25 dB | Fair | May struggle with HD video |
| Below 20 dB | Poor | Expect packet loss and retransmissions |

**Why it causes problems:** Your WiFi adapter needs a minimum SNR to decode data at each speed tier. When SNR drops, the adapter switches to slower encoding, reducing throughput. Below ~15 dB, even the slowest encoding fails intermittently, causing packet loss.

**How to fix:** Either increase signal (move closer, add AP) or reduce noise (change to a less crowded channel, move away from interference sources).

### TX Rate (Mbps) — Transmit Rate

**What:** The maximum data rate your WiFi adapter is currently using to transmit. This is negotiated between your Mac and the access point based on signal conditions.

**Typical values:** TX rate varies hugely by band and standard — 54 Mbps is normal for 2.4 GHz, while 800+ Mbps is typical for WiFi 6 on 5 GHz. Even 20 Mbps is far more than a video call needs (typically 2-4 Mbps), so absolute values don't tell you much. What matters is **relative drops** from your session's peak.

**Why it causes problems:** The TX rate dynamically adjusts — when conditions worsen, the rate drops. A sudden drop from 800 to 200 Mbps means conditions degraded severely. The rate dropping is a proxy indicator: the same conditions causing the rate drop are also causing packet loss and retransmissions. netmon alerts when the rate drops by 50%+ from the session peak, regardless of the absolute value.

### MCS Index — Modulation and Coding Scheme

**What:** A number (0-15 for WiFi 6) that indicates which encoding scheme your WiFi adapter is using. Higher = faster and more efficient, but requires better signal quality.

**Scale:**
| MCS | Encoding | Meaning |
|-----|----------|---------|
| 9-11 | 256-QAM/1024-QAM | Excellent conditions |
| 5-8 | 64-QAM / 256-QAM | Good conditions |
| 3-4 | 16-QAM | Degrading, possible issues |
| 0-2 | BPSK / QPSK | Poor conditions, expect problems |

**Why it causes problems:** MCS drops are an **early warning signal**. Your adapter drops MCS before you see packet loss — it's trying to compensate for worsening conditions by using more robust (but slower) encoding. A drop of 4+ MCS levels in a short time means interference or signal is deteriorating rapidly.

### Channel

**What:** The WiFi channel number your access point is using. Channels map to specific radio frequencies.

- **Channels 1-14:** 2.4 GHz band
- **Channels 32-177:** 5 GHz band

**Why it causes problems:** Channel changes during a session mean your router switched channels (usually to avoid interference or due to a DFS radar event). Each change causes a brief disconnection.

### Channel Band (2.4 GHz vs 5 GHz)

**What:** Which frequency band you're operating on.

| Band | Pros | Cons |
|------|------|------|
| **5 GHz** | Faster, less congested, more channels | Shorter range, blocked by walls |
| **2.4 GHz** | Longer range, penetrates walls | Slower, only 3 non-overlapping channels, crowded |

**Why it causes problems:** If your Mac switches from 5 GHz to 2.4 GHz mid-call, it means the 5 GHz signal became too weak. The 2.4 GHz band is typically much more congested, so you'll likely experience more interference. A band switch is a strong indicator of distance/obstruction problems.

### Channel Width (20/40/80/160 MHz)

**What:** How wide a frequency slice your WiFi connection uses. Wider = faster potential throughput but more susceptible to interference.

| Width | Max throughput | Robustness |
|-------|---------------|------------|
| 20 MHz | Lower | Most robust, least affected by interference |
| 40 MHz | Medium | Good balance |
| 80 MHz | High | Default for 5 GHz WiFi 5/6 |
| 160 MHz | Very high | Vulnerable to interference and DFS |

**Why it causes problems:** Wider channels span more frequencies, which means more chance of overlapping with interfering signals. If you're on 80 or 160 MHz and experiencing signal issues or frequent packet loss, the wide channel is making things worse. Narrowing to 40 MHz sacrifices peak speed but dramatically improves stability.

### DFS Channels (52-64, 100-144)

**What:** Dynamic Frequency Selection channels in the 5 GHz band. These are shared with weather and military radar. When your router detects radar, it **must** evacuate to a non-DFS channel.

**Why it causes problems:** A DFS radar event forces an immediate channel change. Your connection drops for 1-10 seconds while the router scans for a clear channel. There's no way to predict or prevent this — if you're on a DFS channel, it can happen at any time.

**How to fix:** Configure your router to use only non-DFS channels (36, 40, 44, 48 in 5 GHz).

### CCA % — Clear Channel Assessment

**What:** What percentage of time the WiFi channel is busy (occupied by other transmissions). Measured via `wdutil` (requires sudo).

**Scale:**
| Value | Meaning |
|-------|---------|
| 0-20% | Light usage, channel mostly clear |
| 20-40% | Moderate usage, normal for apartments |
| 40-70% | Congested — increased latency and contention |
| Above 70% | Severely congested — expect regular packet loss |

**Why it causes problems:** WiFi is a shared medium. Before your adapter transmits, it listens to check if the channel is clear. When CCA is high, your adapter frequently has to wait (back-off), adding latency. At very high utilization, collisions increase and packets must be retransmitted.

**How to fix:** Switch to a less congested channel. Use the WiFi scan data to find channels with fewer networks.

### AWDL Status — Apple Wireless Direct Link

**What:** Whether macOS's AWDL interface (`awdl0`) is active. AWDL powers AirDrop, Handoff, Sidecar, and other Apple device-to-device features.

**Why it causes problems:** When AWDL is active, your Mac periodically scans for nearby Apple devices. These scans briefly interrupt your main WiFi connection (the radio switches to the AWDL channel and back). This causes periodic latency spikes of 50-200ms, visible as brief stutters in video calls.

netmon only flags AWDL as a problem when it detects correlated latency spikes — AWDL being active alone is normal on most Macs.

**How to fix:** Disable AirDrop and Handoff in System Settings during important calls.

---

## 3. Latency Measurements

These measure how long it takes packets to travel to a destination and back. Low, stable latency is critical for real-time communication.

### Ping (ms) — Internet Round-Trip Time

**What:** Time for a packet to reach the ping target (default: Google DNS at 8.8.8.8) and return. netmon sends 3 pings per sample and records min, average, and max.

**Scale:**
| Value | Quality | Impact on calls |
|-------|---------|-----------------|
| Under 20 ms | Excellent | Imperceptible delay |
| 20-50 ms | Good | Slight delay, fine for conversation |
| 50-100 ms | Fair | Noticeable delay, still usable |
| 100-200 ms | Poor | Visible lag, conversation feels awkward |
| Above 200 ms | Bad | Unusable for real-time communication |

**Why it causes problems:** High latency means your voice/video arrives late. The other person sees you react to things they said a noticeable time ago. Above 150ms, natural conversation becomes difficult because the delay breaks the back-and-forth rhythm.

### Gateway Ping (ms) — Local Network Round-Trip Time

**What:** Time for a packet to reach your router and return. This isolates your local network (WiFi + LAN) from everything beyond.

**Scale:**
| Value | Meaning |
|-------|---------|
| Under 5 ms | Normal, healthy WiFi |
| 5-20 ms | Slightly elevated, WiFi might be congested |
| Above 20 ms | Problem on local network (WiFi interference, router overload) |

**Why it matters for diagnosis:** By comparing gateway ping to internet ping, netmon determines where the problem is:

- **Gateway high + Internet high** → Problem is on your WiFi/local network
- **Gateway low + Internet high** → Problem is your ISP or the internet path
- **Both low** → No latency problem

This distinction is critical because the fix is completely different: WiFi problems need a closer access point or less interference, while ISP problems need a call to your provider.

### Jitter (ms) — Latency Variation

**What:** How much the latency varies between packets. Calculated as the mean absolute deviation of the ping times within each sample.

**Scale:**
| Value | Quality | Impact |
|-------|---------|--------|
| Under 5 ms | Excellent | Smooth audio/video |
| 5-10 ms | Good | Minor, usually imperceptible |
| 10-30 ms | Moderate | Audio may sound choppy, video may stutter |
| Above 30 ms | Bad | Clearly broken audio, frozen video frames |

**Why it causes problems:** Video call applications buffer incoming data to smooth out small variations. But the buffer has limits — if jitter exceeds the buffer size, packets arrive too late to be useful and are discarded. This manifests as choppy audio (syllables dropped) and frozen or jumping video.

Jitter is often worse than constant high latency: a steady 80ms latency is manageable, but latency swinging between 20ms and 150ms causes constant glitches.

### DNS Latency (ms)

**What:** Time to resolve a domain name (google.com) to an IP address.

**Scale:**
| Value | Quality |
|-------|---------|
| Under 30 ms | Normal (local/ISP DNS cache hit) |
| 30-80 ms | Fine, remote DNS server |
| 80-200 ms | Elevated — slow DNS server or network path |
| Above 200 ms | Very slow — misconfigured DNS or network problem |

**Why it causes problems:** DNS isn't continuously used during a video call (the connection is already established). But DNS spikes indicate broader network issues. Slow DNS also affects call setup — joining a meeting may take longer or fail if DNS is unreliable.

---

## 4. Packet Loss

### Loss %

**What:** Percentage of ping packets that never returned. 0% is normal.

**Scale:**
| Value | Impact |
|-------|--------|
| 0% | Normal |
| 0.1-2% | Minor — occasional glitches, usually masked by error correction |
| 2-5% | Noticeable — audio dropouts, video artifacts |
| Above 5% | Severe — call is unusable, audio cuts out frequently |

**Why it causes problems:** Packet loss is the single biggest cause of bad video call quality. Unlike latency (which can be buffered) or bandwidth (which can be adapted), lost packets are simply gone. The application must either wait for retransmission (adding latency) or skip the data (causing glitches).

Video codecs can tolerate about 1% loss. Audio codecs are more sensitive — even 0.5% loss can produce audible artifacts if the lost packets contain voice data.

**Common causes:**
- WiFi interference (the most common cause)
- Router buffer overflow (bufferbloat)
- ISP congestion
- Faulty cables or hardware

---

## 5. Per-Process Traffic

### TCP Traffic (bytes in/out, retransmits, rx_dupe, rx_ooo)

**What:** Per-process network usage measured via macOS `nettop`. Shows which applications are sending and receiving data, and the quality of their TCP connections.

- **bytes_in / bytes_out:** Data volume since the session started
- **retransmits:** Packets that had to be sent again because they were lost or corrupted
- **rx_dupe:** Duplicate packets received (the sender retransmitted but the original also arrived)
- **rx_ooo:** Out-of-order packets (arrived in a different order than sent)

**Why it matters:** High retransmits for your video call process (Zoom, Teams, Meet) directly indicates packet loss on that specific connection. High rx_ooo suggests network path instability (packets taking different routes). Retransmits add latency because TCP waits before retransmitting, and the application must wait for the retransmitted data.

### UDP Traffic (bytes in/out)

**What:** Per-process UDP data volume. Most real-time audio/video uses UDP because it's faster than TCP (no retransmission delays).

**Why it matters:** Your video call application will typically show high UDP traffic (that's the audio/video stream). A sudden drop in UDP bytes for your call app means the stream stalled. UDP has no retransmission — lost UDP packets are simply lost, which is why packet loss directly causes audio/video glitches.

### Per-Connection Details (remote_ip, remote_port, bytes, retransmits)

**What:** Breakdown of each individual TCP connection: where it connects to, how much data it transfers, and its retransmission rate.

**Why it matters:** Lets you see if problems are specific to one server (your video call) or affecting all connections (general network problem).

---

## 6. WiFi Environment Scan

Performed every 30 seconds to survey all visible WiFi networks.

### Scan Data (per network: SSID, BSSID, RSSI, Channel, Security)

**What:** Every WiFi network your Mac can detect, its signal strength, and what channel it's on.

**Why it matters:** The most actionable data for fixing WiFi problems:

- **Networks on your channel:** If 5+ networks share your channel, interference is guaranteed. Switch channels.
- **Strong signals on adjacent channels:** Even networks on nearby channels cause interference (especially in 2.4 GHz where channels overlap).
- **Relative signal strengths:** If neighboring networks are stronger than yours, their transmissions will dominate the channel.

---

## 7. System Health

### CPU Usage (%)

**What:** Total CPU usage across all processes. Can exceed 100% on multi-core systems (e.g., 400% means 4 cores fully loaded).

**Scale:**
| Value | Impact |
|-------|--------|
| Under 150% | Normal |
| 150-300% | Elevated — monitor for thermal throttling |
| Above 300% | High — WiFi driver may be starved for CPU time |

**Why it causes problems:** When CPU is maxed out, the WiFi driver and networking stack compete for processing time. Packets may sit in buffers longer (adding jitter), and the WiFi adapter's time-critical operations (acknowledgments, retransmissions) may be delayed. On laptops, high CPU also causes thermal throttling, which can reduce WiFi radio performance.

### Memory Pressure (%)

**What:** How much of your RAM is actively being used (active + wired + compressor memory as a percentage of total).

**Scale:**
| Value | Impact |
|-------|--------|
| Under 80% | Normal |
| 80-90% | High — system may start swapping |
| Above 90% | Critical — heavy swap activity adds latency to everything |

**Why it causes problems:** When memory pressure is high, macOS starts compressing memory and swapping to disk. This makes everything slower, including the networking stack. Your video call application may not be able to encode/decode video fast enough, causing frames to drop.

### Interface Errors (in/out)

**What:** Cumulative count of errors on your network interface since the monitoring session started. Separate counts for incoming (receive) and outgoing (transmit) errors.

**Why it causes problems:** Interface errors indicate hardware-level problems — corrupted frames that failed the checksum. A small number is normal, but a rapidly increasing count suggests hardware issues (faulty WiFi chipset, driver bugs, or severe interference causing uncorrectable corruption).

---

## Diagnostic Thresholds Summary

netmon continuously evaluates all metrics and raises alerts at these thresholds:

| Metric | Warning | Critical |
|--------|---------|----------|
| RSSI | Below -67 dBm | Below -75 dBm |
| SNR | Below 20 dB | — |
| Ping (5-sample avg) | Above 50 ms | Above 100 ms |
| Jitter (5-sample avg) | Above 10 ms | Above 30 ms |
| Packet Loss (10-sample window) | Any loss detected | >5 of 10 samples have loss |
| DNS (5-sample avg) | Above 80 ms | Above 200 ms |
| TX Rate (3-sample avg) | Dropped 50%+ from peak | Dropped 70%+ from peak |
| Gateway + Internet latency | Internet >3x gateway and >50ms | Gateway >20ms and internet >50ms |
| CPU | Above 200% | Above 400% |
| Memory | Above 80% | Above 90% |
| CCA | Above 40% | Above 70% |
| Channel width (80/160 MHz) | Signal issues or 3+ loss events in last 10 | — |
| MCS drop | Drop of 4+ to below MCS 5 | — |
| Band switch | 5 GHz to 2.4 GHz detected | — |
| DFS channel | Currently on DFS channel | — |
| AP roaming | Multiple BSSIDs seen | — |
| Channel congestion (from scan) | 2+ networks on same channel | 4+ networks on same channel |
| AWDL | Active with 2+ latency spikes in last 10 samples | — |

---

## Reading the Data: What to Look For

### Pattern: Periodic spikes every 30-60 seconds
**Likely cause:** AWDL scanning, background app polling, or WiFi scanning by the OS.

### Pattern: Latency spike + packet loss at same time
**Likely cause:** WiFi interference event. Check if RSSI/SNR also dipped — if yes, something temporarily blocked or interfered with the signal.

### Pattern: Gateway ping is fine, internet ping is bad
**Likely cause:** ISP problem or internet congestion. Nothing wrong with your WiFi.

### Pattern: Gateway ping is also bad
**Likely cause:** Local WiFi problem. Check RSSI, SNR, CCA, and channel congestion.

### Pattern: MCS drops, then TX rate drops, then packet loss starts
**Likely cause:** Gradual signal degradation (someone closing a door, microwave turning on). The MCS drop is the earliest warning.

### Pattern: Sudden channel change + everything breaks for a few seconds
**Likely cause:** DFS radar event forced a channel evacuation.

### Pattern: Band switch from 5 GHz to 2.4 GHz
**Likely cause:** You moved too far from the router for 5 GHz. The 2.4 GHz connection will be slower and more congested but has longer range.
