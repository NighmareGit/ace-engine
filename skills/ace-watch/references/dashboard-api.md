# Dashboard API Reference

All endpoints require bearer-token auth when configured.

## POST /api/inject
Inject custom events into the stream.
Body: {type, data}
Valid types: system.annotation, system.panel, system.highlight, system.focus, system.command

## GET /api/config
Get current dashboard configuration.

## POST /api/config
Update dashboard configuration.
Body: {highlight_tasks, focus_source, auto_scroll, custom_panels, annotations}

## POST /api/panel
Add/update a custom panel.
Body: {title, content, position}
Returns: {ok, panel: {id, title, content, position}}

## DELETE /api/panel/{panel_id}
Remove a custom panel.
