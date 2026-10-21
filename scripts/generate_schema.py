import json
from pathlib import Path

from schema.approval import ApprovalContracts
from schema.assembly import AssemblyContracts
from schema.gazette import GazetteContracts
from schema.identity import IdentityContracts
from schema.jurisdictions import JurisdictionProfile
from schema.memberships import MembershipRegister
from schema.models import Corpus
from schema.operator import OperatorContracts
from schema.policy import PolicyContracts
from schema.public import PublicContracts
from schema.reader import ReaderDescriptor
from schema.record import DocumentRecord

if __name__ == "__main__":
    Path("schema/gazette.schema.json").write_text(
        json.dumps(GazetteContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/approval.schema.json").write_text(
        json.dumps(ApprovalContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/corpus.schema.json").write_text(json.dumps(Corpus.model_json_schema(), indent=2) + "\n")
    Path("schema/identity.schema.json").write_text(
        json.dumps(IdentityContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/policy.schema.json").write_text(
        json.dumps(PolicyContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/public.schema.json").write_text(
        json.dumps(PublicContracts.model_json_schema(mode="serialization"), indent=2) + "\n"
    )
    Path("schema/operator.schema.json").write_text(
        json.dumps(OperatorContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/assembly.schema.json").write_text(
        json.dumps(AssemblyContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/reader.schema.json").write_text(
        json.dumps(ReaderDescriptor.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/record.schema.json").write_text(
        json.dumps(DocumentRecord.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/jurisdiction.schema.json").write_text(
        json.dumps(JurisdictionProfile.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/membership.schema.json").write_text(
        json.dumps(MembershipRegister.model_json_schema(), indent=2) + "\n"
    )
