import { PageHeader } from '../components/ui';
import CopilotConsole from '../features/copilot/CopilotConsole';

export default function AiCopilotPage() {
  return (
    <div>
      <PageHeader
        title="AI Copilot"
        description="Operate the Retention Engine in plain English. Reads run straight away; every change is previewed and runs only when you confirm it."
      />
      <CopilotConsole />
    </div>
  );
}
