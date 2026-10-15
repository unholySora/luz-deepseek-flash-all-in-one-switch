"""Structured v2: keep the counting task longer than the requested output budget."""
import hashlib
import re

VERSION='structured-v2'

def build_prompt(output_tokens,conc,index,wave,tag):
    assert isinstance(output_tokens,int) and output_tokens>0
    maximum=output_tokens
    nonce=hashlib.sha256(('%s|%d|%d|%d|%d'%(tag,output_tokens,conc,index,wave)).encode()).hexdigest()[:12]
    return ('[sd1:structured-v2:%s c%d w%d r%d] '%(nonce,conc,wave,index)+
        'Count upward from 1 to %d. Output only the numbers in ascending order, separated by single spaces. '
        'Do not repeat or restart the sequence. Do not add code, markup, or explanations.'%maximum)

def build_suite(output_tokens,waves,tag):
    return {'structured/c%d/w%d/r%d'%(c,w,i):build_prompt(output_tokens,c,i,w,tag)
            for c in [1,2,4,8] for w in range(waves) for i in range(c)}

def check_output(text,maximum,full_length):
    text=text.strip()
    numeric_only=bool(re.fullmatch(r'[0-9]+(?:\s+[0-9]+)*',text))
    pieces=text.split() if numeric_only else []
    preceding=bool(pieces) and all(s==str(i) for i,s in enumerate(pieces[:-1],1))
    final_expected=str(len(pieces))
    final_complete=bool(pieces) and pieces[-1]==final_expected
    final_prefix=bool(pieces) and full_length and final_expected.startswith(pieces[-1])
    correct=preceding and (final_complete or final_prefix) and len(pieces)<=maximum
    return {'version':VERSION,'count_max':maximum,'numeric_only':numeric_only,
        'ascending_sequence_correct':correct,'numbers_emitted':len(pieces),
        'last_number':pieces[-1] if pieces else None,
        'last_number_may_be_token_limit_prefix':bool(correct and not final_complete),
        'single_space_format':numeric_only and bool(re.fullmatch(r'[0-9]+(?: [0-9]+)*',text))}
