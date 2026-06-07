# ha-agent

Local-first home automation agent for Home Assistant.

Talks to Home Assistant (HAOS/Supervised) over its REST + WebSocket API and to
Ollama for inference (a small model on the HA device, a larger model on a LAN
box, with routing and offline fallback). Designed to be sensor-aware before
acting, to learn household habits over time, and to be controlled in natural
language — text first, voice (via HA Assist) later. Packaged as a Home Assistant
add-on.

> Status: early scaffolding. See the planning notes / implementation plan for the
> architecture and phased build-out.
