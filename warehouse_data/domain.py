"""核对原始配置及数量，不执行仓库变换。"""
from .io import integer,unique
def summarize(v,root):
    unique(v['candidates'],'id');unique(v['queries'],'id');integer(v['storage_budget'])
    for q in v['queries']:
        if not q['plans']:raise ValueError('查询方案为空')
    return {'candidates':len(v['candidates']),'queries':len(v['queries']),'mandatory':len(v['mandatory'])}
