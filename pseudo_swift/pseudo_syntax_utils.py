from pygments.lexers import SwiftLexer
from pygments.token import Comment, Keyword, Name, Number, Punctuation, String

from binaryninja import InstructionTextTokenType


_LEXER = SwiftLexer()

# Modern Swift stdlib types Pygments' SwiftLexer doesn't classify as Name.Builtin.
_EXTRA_SWIFT_BUILTINS: frozenset[str] = frozenset(
    {
        "UnsafeMutableRawPointer",
        "UnsafeRawPointer",
        "OpaquePointer",
        "UnsafeRawBufferPointer",
        "UnsafeMutableRawBufferPointer",
        "Set",
        "Result",
        "ClosedRange",
        "Substring",
    }
)


def _pygments_to_bn(pyg_token, text: str) -> InstructionTextTokenType:
    if text.startswith("@"):
        return InstructionTextTokenType.AnnotationToken
    if pyg_token in Comment:
        return InstructionTextTokenType.CommentToken
    if pyg_token in String:
        return InstructionTextTokenType.StringToken
    if pyg_token in Number.Float:
        return InstructionTextTokenType.FloatingPointToken
    if pyg_token in Number:
        return InstructionTextTokenType.IntegerToken
    if pyg_token in Keyword.Type:
        return InstructionTextTokenType.TypeNameToken
    if pyg_token in Keyword:
        return InstructionTextTokenType.KeywordToken
    if pyg_token in Name.Decorator or pyg_token in Name.Attribute:
        return InstructionTextTokenType.AnnotationToken
    if pyg_token in Name.Builtin or pyg_token in Name.Class:
        return InstructionTextTokenType.TypeNameToken
    if pyg_token in Name and text in _EXTRA_SWIFT_BUILTINS:
        return InstructionTextTokenType.TypeNameToken
    return InstructionTextTokenType.TextToken


def _merge_attribute_tokens(raw: list[tuple]) -> list[tuple]:
    """Collapse `@`+identifier pairs (e.g. `@_cdecl`, `@MainActor`) that Pygments doesn't allow-list."""
    result: list[tuple] = []
    i = 0
    n = len(raw)
    while i < n:
        offset, ttype, text = raw[i]
        if text == "@" and ttype in Punctuation and i + 1 < n:
            next_off, next_ttype, next_text = raw[i + 1]
            if next_off == offset + 1 and next_ttype in Name and next_text:
                result.append((offset, Name.Decorator, "@" + next_text))
                i += 2
                continue
        result.append(raw[i])
        i += 1
    return result


def _tokenize_swift_line(
    line: str,
) -> list[tuple[str, InstructionTextTokenType]]:
    raw = list(_LEXER.get_tokens_unprocessed(line))
    merged = _merge_attribute_tokens(raw)
    result: list[tuple[str, InstructionTextTokenType]] = []
    for _, pyg_token, text in merged:
        if not text or text == "\n":
            continue
        result.append((text, _pygments_to_bn(pyg_token, text)))
    return result
