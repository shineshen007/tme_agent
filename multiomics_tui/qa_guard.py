"""TUI-only summary contract. These checks reduce risk, not prove entailment."""
import re

GUARD_VERSION = 'tui-summary-bilingual-v4'
CITATION = re.compile(r'\[(S\d+)\]')
TOKEN = re.compile(r'(?<![A-Za-z0-9_])[A-Za-z0-9][A-Za-z0-9_.:-]*(?![A-Za-z0-9_])')
IDENTIFIER = re.compile(r'(?:[A-Z][A-Z0-9_-]{1,30}|[0-9]+[A-Z][A-Z0-9_-]*|GO:\d{7}|'
                        r'(?:GOBP|MSigDB|GO:BP|v)[A-Za-z0-9_.:-]*\d[A-Za-z0-9_.:-]*)')
STAT_NAMES = re.compile(r'^(?:HR|OR|CI|FDR|P|N|NES)\d', re.I)
ENGLISH_QUANTITY = re.compile(r'\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|hundred|thousand|million|billion|first|second|third|dozen|percent|percentage)\b', re.I)
ENGLISH_CAUSAL = re.compile(r'\b(?:causes?|caused|driv(?:e|es|en)|mediates?|mediated|regulates?|regulated|prov(?:e|es|ed|en)|demonstrat(?:e|es|ed)|establish(?:es|ed))\b', re.I)
ENGLISH_HEDGE = re.compile(r'\b(?:may|might|could|suggests?|candidate|hypothes(?:is|ized)|observational|associated|correlat(?:ed|ion)|not|no|without|uncertain)\b', re.I)

CHINESE_QUANTITY = re.compile(
    r'[零〇一二三四五六七八九十百千万亿两]{2,}|'
    r'[零〇一二三四五六七八九十百千万亿两](?:个|名|例|条|种|组|项|倍|成|人|次|年|月|天|对|分之)|'
    r'(?:共|为|有|占|达|约|超过|少于|至少|至多)[零〇一二三四五六七八九十百千万亿两](?=[，。；\s]|$)')
VALIDATION_CLAIM = re.compile(r'(?:已(?:经)?(?:获得|得到|完成|通过)?\s*)?独立验证|独立预后因子|'
                              r'(?:independent(?:ly)?|externally)\s+(?:validated|validation|prognostic\s+(?:factor|marker|predictor))|'
                              r'validated\s+in\s+an?\s+independent\s+cohort', re.I)
NEGATION = re.compile(r'(?:未|尚未|未经|没有|不能|不构成|不支持|不代表|不属于|不是|并非|不得宣称|不应宣称)'
                      r'(?:获得|得到|完成|通过|进行|经过|作为|称为|被|是|已|的|了|\s)*$|'
                      r'(?:not|no|without)(?:\s+(?:yet|been|an?))*\s*$', re.I)


def validate_tui_summary(answer, citations, chunks, language="en"):
    from orchestrator.checkers import validate_language
    known = {c.cid for c in chunks}
    if not isinstance(citations, list) or not citations or not all(isinstance(c, str) for c in citations):
        raise ValueError('Summary requires nonempty citations')
    mentioned = set(CITATION.findall(answer))
    if set(citations) != mentioned or not mentioned <= known:
        raise ValueError('Answer citations must match the supplied excerpts')
    source = '\n'.join(c.text for c in chunks)
    source_tokens = {token.rstrip('.:-') for token in TOKEN.findall(source)}
    has_chinese = bool(re.search(r'[\u3400-\u9fff]', answer))
    if language == 'en' and has_chinese:
        raise ValueError('AI summary must be in English')
    if language == 'zh' and not has_chinese:
        raise ValueError('AI summary must be in Chinese')
    cleaned = CITATION.sub(' ', answer)

    def strip_identifier(match):
        word = match.group().rstrip('.:-')
        if (word in source_tokens and IDENTIFIER.fullmatch(word) and not STAT_NAMES.match(word)):
            return ' '
        return word

    cleaned = TOKEN.sub(strip_identifier, cleaned)
    if re.search(r'\d', cleaned) or CHINESE_QUANTITY.search(cleaned) or ENGLISH_QUANTITY.search(cleaned):
        raise ValueError('Summary contains numeric claims; cite the original excerpt')
    # Clauses rather than a whole answer prevent an unrelated qualifier from
    # licensing a causal assertion elsewhere in the answer.
    for sentence in re.split(r'[。！？!?；;\n，,]|(?<!\d)\.(?!\d)', answer):
        validate_language({'answer': sentence})
        if ENGLISH_CAUSAL.search(sentence) and not ENGLISH_HEDGE.search(sentence):
            raise ValueError('Summary contains unqualified causal language')
        for match in VALIDATION_CLAIM.finditer(sentence):
            if not NEGATION.search(sentence[:match.start()]):
                raise ValueError('Summary cannot claim independent validation or prognostic status')
