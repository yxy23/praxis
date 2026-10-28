
import difflib
import re

BASE = frozenset({'/help', '/status', '/exit', '/quit'})

def body(text):
    text = text.strip()
    if text.startswith('You are answering one prompt inside '):
        text = text.partition("\n\nUser's prompt: ")[2].strip()
    return text

def token(text):
    parts = body(text).split()
    return parts[0] if parts and re.fullmatch(r'/[A-Za-z][A-Za-z0-9_-]*', parts[0]) else ''

def known(surface, room=None):
    import runner
    lifecycle = runner.leo_lifecycle_commands()
    if surface in {'send', 'convene'}:
        return frozenset({'/help', '/adaptive'}) | (lifecycle if surface == 'send' else set())
    if surface == 'room-command':
        return frozenset({'/help', '/adaptive'} | ({'/plan', '/decide'} if room.planning_template and not room.live else set()))
    if surface == 'live':
        return BASE | {'/sources', '/adaptive'}
    if surface == 'ordinary':
        return BASE | {'/trace', '/adaptive'} | (lifecycle if room.chair == 'Leo' else set()) | ({'/plan', '/decide'} if room.planning_template else set())
    return BASE | {'/adaptive'} | lifecycle

def help_text(surface, room=None):
    return 'Commands here: ' + ' · '.join(sorted(known(surface, room)))

def refusal(text, surface, room=None):
    command = token(text)
    if not command:
        return ''
    import runner
    if surface in {'send', 'leo'} and runner.leo_lifecycle(body(text)):
        return ''
    commands = known(surface, room)
    invalid_lifecycle = command in runner.leo_lifecycle_commands() and not runner.leo_lifecycle(body(text))
    if invalid_lifecycle:
        return 'Invalid lifecycle command arguments. '+help_text(surface,room)+'. Nothing was dispatched.'
    if command not in commands:
        suggestion = difflib.get_close_matches(command, sorted(commands), n=1, cutoff=0.6)
        hint = ' — did you mean '+suggestion[0]+'?' if suggestion else ''
        return 'Unknown command '+command+hint+' '+help_text(surface, room)+'. Nothing was dispatched.'
    if command in {'/help', '/status', '/trace', '/sources', '/exit', '/quit'} and body(text) != command:
        return command+' takes no arguments. Nothing was dispatched.'
    return ''
