# recall 路径 cProfile 热点报告

- 语料：300 条；查询：60 轮；墙钟：0.08s（734.1 qps）

```
         311317 function calls (299016 primitive calls) in 0.082 seconds

   Ordered by: cumulative time
   List reduced from 220 to 25 due to restriction <25>

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000    0.082    0.082 /Volumes/移动硬盘/记忆系统重构/tests/profile_recall.py:58(run_queries)
       60    0.001    0.000    0.058    0.001 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:916(build)
       78    0.000    0.000    0.052    0.001 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger/search.py:234(search)
       78    0.006    0.000    0.052    0.001 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger/search.py:251(_search_impl)
      198    0.016    0.000    0.016    0.000 {method 'execute' of 'sqlite3.Connection' objects}
     1140    0.000    0.000    0.010    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:391(_fold_key)
     1470    0.001    0.000    0.009    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:281(_normalised_text_key)
      761    0.005    0.000    0.009    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger/search.py:217(_row_dict)
       16    0.000    0.000    0.006    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:577(_deepen_until_distinct)
       16    0.000    0.000    0.006    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:594(distinct_count)
       16    0.000    0.000    0.006    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:595(<setcomp>)
     1102    0.000    0.000    0.005    0.000 {built-in method builtins.any}
40415/28939    0.002    0.000    0.005    0.000 {built-in method builtins.isinstance}
       16    0.000    0.000    0.005    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:629(_fold_duplicate_candidates)
    21341    0.005    0.000    0.005    0.000 {method 'sub' of 're.Pattern' objects}
      791    0.001    0.000    0.005    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger/_helpers.py:243(_sim_tokens)
     1470    0.001    0.000    0.005    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/textutil.py:109(normalise_for_dedup)
     5171    0.001    0.000    0.004    0.000 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/re.py:203(sub)
      649    0.000    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:1044(<genexpr>)
      319    0.000    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/textutil.py:87(token_sim)
       99    0.004    0.000    0.004    0.000 {method 'fetchall' of 'sqlite3.Cursor' objects}
     5738    0.001    0.000    0.004    0.000 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/typing.py:709(__instancecheck__)
      761    0.002    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger/_helpers.py:253(_context_search_text)
       11    0.000    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger/lifecycle.py:88(touch)
     2283    0.001    0.000    0.004    0.000 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/json/__init__.py:299(loads)



```
