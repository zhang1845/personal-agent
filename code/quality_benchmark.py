"""Reproducible self-authored sample set, never imported into personal storage."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from personal_agent.knowledge import KnowledgeStore
from personal_agent.personal_store import PersonalStore
from personal_agent.quality import QualityStore


def benchmark():
    with TemporaryDirectory(prefix='检索评测 ') as folder:
        root=Path(folder);library=KnowledgeStore(root/'knowledge.sqlite3');store=PersonalStore(root/'assistant.sqlite3')
        quality=QualityStore(root,store,library);project=library.save_project({'name':'自编示例资料','background':'非真实用户资料，仅验证检索方法'})
        materials={
            'meeting':('会议安排','会议安排在工作日九点至十八点。午休十二点至十三点。会议前后预留十五分钟缓冲。'),
            'refund':('退款办理','普通说明。'*180+'退款资格：七日内未拆封可申请退款，退款凭证需要星图订单编号，到账等待七个工作日。'),
            'leave':('请假手续','请假材料包括请假申请单和交接记录，至少提前两个工作日提交，经过直属主管审批。'),
            'expense':('差旅报销','差旅报销材料包括发票和行程单，当月月底前提交，财务在审核通过后付款。'),
            'archive':('资料归档','网页材料先提取正文和来源，上传文件先解析预览。项目资料应保留原文与个人想法的区别。'),
        }
        sources={key:library.save_source({'title':title,'content':content,'kind':'material','project_id':project['id']}) for key,(title,content) in materials.items()}
        questions=[('工作时段','会议安排的工作时段',['meeting']),('午休时段','会议安排避开午休',['meeting']),
            ('缓冲规则','会议缓冲预留多少分钟',['meeting']),('退款凭证','退款凭证需要什么',['refund']),
            ('退款资格','七日未拆封退款资格',['refund']),('请假材料','请假需要准备什么材料',['leave']),
            ('审批流程','请假由谁审批',['leave']),('报销材料','差旅报销需要发票和什么',['expense']),
            ('截止时间','差旅报销何时提交',['expense']),('资料区分','原文与个人想法应如何归档',['archive']),
            ('多份资料','退款凭证和差旅报销材料',['refund','expense']),('无资料问题','火星企鹅航班',[])]
        for title,query,expected in questions:
            quality.save_case({'title':title,'query':query,'project_id':project['id'],'kind':'answerable' if expected else 'unanswerable','expected_source_ids':[sources[x]['id'] for x in expected]})
        report=quality.evaluate({'top_k':3})
        report.update(dataset='自编固定示例集 v1（12例）',scope='仅检索评测；不代表个人资料效果、模型回答正确率或真实用户收益。')
        return report


if __name__=='__main__':
    from personal_agent.platform_utils import configure_stdio
    configure_stdio()
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('output/retrieval-benchmark.json'));args=parser.parse_args()
    report=benchmark();args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'dataset':report['dataset'],'results':{name:value['metrics'] for name,value in report['engines'].items()}},ensure_ascii=False))
