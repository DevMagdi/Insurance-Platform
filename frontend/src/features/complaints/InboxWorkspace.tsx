import { useState } from 'react';
import { Icon } from '../../components/Icon';
import type { ComplaintFilterOptions, DateRange, StaffAccess } from '../../types';
import { ComplaintQueue } from './ComplaintQueue';
import { TicketWorkspace } from './TicketWorkspace';

interface InboxWorkspaceProps {
  access: StaffAccess;
  range: DateRange;
  options: ComplaintFilterOptions;
  companyNames: Map<string, string>;
  onOpenTicket: (id: string) => void;
  onRefreshAccess: () => Promise<void>;
}

/** A focused conversation desk, while the full table remains one click away. */
export function InboxWorkspace({ access, range, options, companyNames, onOpenTicket, onRefreshAccess }: InboxWorkspaceProps) {
  const [selectedId, setSelectedId] = useState('');
  const [showFullQueue, setShowFullQueue] = useState(false);

  if (showFullQueue) {
    return (
      <div className="page-stack inbox-full-queue">
        <button className="inbox-return-button" type="button" onClick={() => setShowFullQueue(false)}>
          <Icon name="arrow" size={15} /> العودة إلى مساحة المحادثة
        </button>
        <ComplaintQueue access={access} range={range} options={options} companyNames={companyNames} onOpenTicket={onOpenTicket} />
      </div>
    );
  }

  return (
    <div className="inbox-workspace" aria-label="مساحة صندوق الطلبات">
      {selectedId ? (
        <TicketWorkspace
          id={selectedId}
          access={access}
          options={options}
          compactInbox
          onBack={() => setSelectedId('')}
          onOpenTicket={setSelectedId}
          onRefreshAccess={onRefreshAccess}
        />
      ) : (
        <>
          <aside className="inbox-selection-empty" aria-label="تفاصيل الطلب"><Icon name="inbox" size={22} /><span>اختر طلباً لعرض بياناته</span></aside>
          <section className="inbox-conversation-empty"><Icon name="message" size={26} /><h2>مساحة المحادثة</h2><p>اختر طلباً من القائمة لعرض الرسائل والتحديثات والرد عليها.</p></section>
        </>
      )}
      <ComplaintQueue
        access={access}
        range={range}
        options={options}
        companyNames={companyNames}
        compact
        selectedTicket={selectedId}
        onSelectTicket={setSelectedId}
        onExpand={() => setShowFullQueue(true)}
        onOpenTicket={onOpenTicket}
      />
    </div>
  );
}
