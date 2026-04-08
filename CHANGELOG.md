# Changelog

## [0.2.0] - 2026-04-08
### Added
- Dedicated documentation guides under `docs/`:
	- `event-listeners.md`
	- `observability.md`
	- `retry-timeout-rate-limit.md`
- Expanded event-listener documentation including scoping guidance, chaining examples, and common pitfalls.

### Changed
- Event listeners are now the primary public model for orchestration and observability.
- Tracing and stats ownership moved into `Events` (`dvt.events.trace*` and `dvt.events.stats()`).
- README reorganized as a concise overview with links to detailed guides.

### Removed
- `after_end(...)` event-chaining API.
- `status(...)` event status polling API.
- `Dovetail` trace/stats alias methods; use `dvt.events.*` directly.

## [0.1.0] - 2026-04-02
### Added
- Initial public release with `Dovetail.task` helpers and basic tests.

### Notes
- This is an initial release; API may change in minor versions.