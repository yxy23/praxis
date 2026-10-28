# Shared room presentation

Leo, context/project rooms, retained live rooms and legacy one-discussion rooms
use `room_ui.py`. User messages have a heavy left rail and `YOU` heading. Agent
responses have a light rail, an explicit name, and the same turn number as the
question. Questions for the user are labeled separately. Colour is optional;
`--plain` retains all structural distinctions and clickable links.

Interactive rooms use a wrapped, editable input buffer. Enter submits, Esc then
Enter inserts a line, and bracketed multiline paste stays one message. History
is in memory for the current room only. Background answers print above an
unfinished draft. No alternate screen is used, so terminal scrollback remains
available. Width is measured for each new output block; already-printed
scrollback is not retroactively reformatted when a pane is resized.

Formatting is a terminal view only. Saved prompts, events, routing, transcripts,
worker lifecycle and restart behaviour do not change. Piped output keeps
Markdown targets intact. Link labels wrap by visible cells; OSC-8 destinations
are never split, and links close before margins/newlines.

The shared transport also recognizes exact existing file mentions in prose:
absolute paths and `~/...` paths, including spaces and
Korean filenames. Missing files, directories and ambiguous filenames stay
unlinked. Rendering twice preserves existing OSC-8 links, and no delivery
pointers are created for piped output.

For runtime configuration and the scope of offline verification, see
`../praxis45/PUBLIC-RUNTIME.md`.
