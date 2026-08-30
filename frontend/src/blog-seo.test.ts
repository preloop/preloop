import { expect } from '@open-wc/testing';
import type { BrandConfig } from './brand-config';
import {
  BLOG_BASE_PATH,
  buildBlogPostingSchema,
  buildBlogSchema,
  buildBlogBreadcrumbSchema,
  estimate_reading_minutes,
  format_display_date,
  generate_blog_feed_xml,
  generate_blog_llms_section,
  get_blog_index_meta,
  get_blog_post_meta,
  get_blog_routes,
  get_blog_slug_from_route,
  get_internal_link_label,
  get_published_posts,
  is_blog_enabled,
  render_blog_index_html,
  render_blog_post_html,
  sort_posts,
  strip_duplicate_title_heading,
  strip_leading_markdown_title,
  to_rfc822,
  type BlogPost,
} from './blog-seo';

const config = {
  name: 'Preloop',
  domain: 'preloop.ai',
  edition: 'saas',
  company: { legal_name: 'Preloop Inc.' },
  landing: {
    meta: {
      description: 'The open-source AI agent control plane.',
      keywords: 'AI agent control plane, MCP firewall',
      og_image: '/images/diagram.png',
    },
  },
} as unknown as BrandConfig;

const selfhosted = { ...config, edition: 'selfhosted' } as BrandConfig;

function makePost(overrides: Partial<BlogPost> = {}): BlogPost {
  return {
    slug: 'a-post',
    title: 'A Post',
    description: 'A description.',
    date: '2026-07-18',
    author: 'Dimitris Moraitis',
    tags: ['MCP firewall'],
    related: ['/vs/litellm'],
    reading_minutes: 6,
    body_html: '<p>Body.</p>',
    ...overrides,
  };
}

describe('blog-seo', () => {
  describe('edition gating', () => {
    it('enables the blog on saas builds', () => {
      expect(is_blog_enabled(config)).to.be.true;
    });

    it('disables the blog on self-hosted builds', () => {
      expect(is_blog_enabled(selfhosted)).to.be.false;
    });

    it('emits no routes for self-hosted builds', () => {
      expect(get_blog_routes(selfhosted, [makePost()])).to.deep.equal([]);
    });

    it('emits index plus post routes for saas builds', () => {
      expect(get_blog_routes(config, [makePost()])).to.deep.equal([
        '/blog',
        '/blog/a-post',
      ]);
    });

    it('emits nothing when there are no posts', () => {
      expect(get_blog_routes(config, [])).to.deep.equal([]);
    });
  });

  describe('get_blog_slug_from_route', () => {
    it('extracts a valid slug', () => {
      expect(get_blog_slug_from_route('/blog/watch-approvals')).to.equal(
        'watch-approvals'
      );
    });

    it('rejects the index route', () => {
      expect(get_blog_slug_from_route('/blog')).to.be.null;
    });

    it('rejects nested paths', () => {
      expect(get_blog_slug_from_route('/blog/a/b')).to.be.null;
    });

    it('rejects traversal and uppercase input', () => {
      expect(get_blog_slug_from_route('/blog/../secret')).to.be.null;
      expect(get_blog_slug_from_route('/blog/-leading')).to.be.null;
    });

    it('rejects non-blog routes', () => {
      expect(get_blog_slug_from_route('/vs/litellm')).to.be.null;
    });
  });

  describe('ordering', () => {
    it('sorts newest first', () => {
      const sorted = sort_posts([
        makePost({ slug: 'old', date: '2026-01-01' }),
        makePost({ slug: 'new', date: '2026-07-01' }),
      ]);
      expect(sorted.map((post) => post.slug)).to.deep.equal(['new', 'old']);
    });

    it('breaks ties on slug so ordering is deterministic', () => {
      const sorted = sort_posts([
        makePost({ slug: 'b' }),
        makePost({ slug: 'a' }),
      ]);
      expect(sorted.map((post) => post.slug)).to.deep.equal(['a', 'b']);
    });

    it('drops drafts', () => {
      const posts = get_published_posts([
        makePost({ slug: 'live' }),
        makePost({ slug: 'wip', draft: true }),
      ]);
      expect(posts.map((post) => post.slug)).to.deep.equal(['live']);
    });
  });

  describe('formatting', () => {
    it('formats an ISO date for display', () => {
      expect(format_display_date('2026-07-18')).to.equal('18 July 2026');
    });

    it('passes malformed dates through unchanged', () => {
      expect(format_display_date('not-a-date')).to.equal('not-a-date');
    });

    it('produces an RFC 822 pubDate', () => {
      expect(to_rfc822('2026-07-18')).to.contain('18 Jul 2026');
    });

    it('estimates reading time with a floor of one minute', () => {
      expect(estimate_reading_minutes('one two three')).to.equal(1);
      expect(
        estimate_reading_minutes(new Array(600).fill('word').join(' '))
      ).to.equal(3);
    });
  });

  describe('route metadata', () => {
    it('builds index metadata carrying the brand name', () => {
      const meta = get_blog_index_meta(config);
      expect(meta.title).to.contain('Preloop');
      expect(meta.description).to.contain('open-source AI agent control plane');
    });

    it('derives post metadata from frontmatter', () => {
      const meta = get_blog_post_meta(makePost(), config);
      expect(meta.title).to.equal('A Post | Preloop');
      expect(meta.description).to.equal('A description.');
      expect(meta.og_title).to.equal('A Post');
    });

    it('merges post tags into keywords ahead of the brand defaults', () => {
      const meta = get_blog_post_meta(makePost(), config);
      expect(meta.keywords.indexOf('MCP firewall')).to.equal(0);
      expect(meta.keywords).to.contain('AI agent control plane');
    });

    it('falls back to the brand og_image', () => {
      expect(get_blog_post_meta(makePost(), config).og_image).to.equal(
        '/images/diagram.png'
      );
    });
  });

  describe('structured data', () => {
    it('builds a BlogPosting with absolute url and both dates', () => {
      const schema = buildBlogPostingSchema(
        makePost({ updated: '2026-07-20' }),
        config
      ) as Record<string, any>;
      expect(schema['@type']).to.equal('BlogPosting');
      expect(schema.url).to.equal('https://preloop.ai/blog/a-post');
      expect(schema.datePublished).to.equal('2026-07-18');
      expect(schema.dateModified).to.equal('2026-07-20');
      expect(schema.image).to.equal('https://preloop.ai/images/diagram.png');
    });

    it('defaults dateModified to the publish date', () => {
      const schema = buildBlogPostingSchema(makePost(), config) as Record<
        string,
        any
      >;
      expect(schema.dateModified).to.equal('2026-07-18');
    });

    it('names a Person author so posts are founder-signed', () => {
      const schema = buildBlogPostingSchema(makePost(), config) as Record<
        string,
        any
      >;
      expect(schema.author['@type']).to.equal('Person');
      expect(schema.author.name).to.equal('Dimitris Moraitis');
    });

    it('builds a Blog schema listing every published post', () => {
      const schema = buildBlogSchema(config, [
        makePost({ slug: 'one', date: '2026-07-01' }),
        makePost({ slug: 'two', date: '2026-07-02' }),
        makePost({ slug: 'draft', draft: true }),
      ]) as Record<string, any>;
      expect(schema['@type']).to.equal('Blog');
      expect(schema.blogPost).to.have.lengthOf(2);
      expect(schema.blogPost[0].url).to.equal('https://preloop.ai/blog/two');
    });

    it('builds a three-level breadcrumb', () => {
      const schema = buildBlogBreadcrumbSchema(makePost(), config) as Record<
        string,
        any
      >;
      expect(schema.itemListElement).to.have.lengthOf(3);
      expect(schema.itemListElement[2].item).to.equal(
        'https://preloop.ai/blog/a-post'
      );
    });
  });

  describe('rendered fragments', () => {
    it('renders a post with a dateline, tags and related links', () => {
      const html = render_blog_post_html(makePost(), config, '');
      expect(html).to.contain('<h1>A Post</h1>');
      expect(html).to.contain('datetime="2026-07-18"');
      expect(html).to.contain('Dimitris Moraitis');
      expect(html).to.contain('6 min read');
      expect(html).to.contain('MCP firewall');
      expect(html).to.contain('href="/vs/litellm"');
      expect(html).to.contain('Preloop vs Litellm');
      expect(html).to.contain('<p>Body.</p>');
    });

    it('omits the related block when there are no related links', () => {
      const html = render_blog_post_html(makePost({ related: [] }), config, '');
      // The `.blog-related` CSS rule is always present in the style block;
      // assert on the rendered element instead.
      expect(html).to.not.contain('<aside class="blog-related">');
      expect(html).to.not.contain('Related reading');
    });

    it('escapes titles rather than emitting raw markup', () => {
      const html = render_blog_post_html(
        makePost({ title: '<script>x</script>' }),
        config,
        ''
      );
      expect(html).to.not.contain('<script>x</script>');
      expect(html).to.contain('&lt;script&gt;');
    });

    it('does not repeat a markdown h1 that matches the frontmatter title', () => {
      const html = render_blog_post_html(
        makePost({
          title: 'Preloop 0.15.0: flow schedules',
          body_html: '<h1>Preloop 0.15.0: flow schedules</h1>\n<p>Shipped.</p>',
        }),
        config,
        ''
      );
      expect(html.match(/<h1>/g)).to.have.lengthOf(1);
      expect(html).to.contain('<p>Shipped.</p>');
    });

    it('keeps a leading h1 that is not the post title', () => {
      const html = render_blog_post_html(
        makePost({
          body_html: '<h1>A subsection</h1><p>Body.</p>',
        }),
        config,
        ''
      );
      expect(html).to.contain('<h1>A Post</h1>');
      expect(html).to.contain('<h1>A subsection</h1>');
    });

    it('renders og_image as a hero figure', () => {
      const html = render_blog_post_html(
        makePost({ og_image: '/assets/blog/ship.png' }),
        config,
        ''
      );
      expect(html).to.contain('class="blog-hero"');
      expect(html).to.contain('src="/assets/blog/ship.png"');
      expect(html).to.contain('alt=""');
      expect(html).to.not.match(/alt="A Post"/);
    });

    it('renders the index newest-first with links to each post', () => {
      const html = render_blog_index_html(
        config,
        [
          makePost({ slug: 'one', title: 'One', date: '2026-07-01' }),
          makePost({ slug: 'two', title: 'Two', date: '2026-07-02' }),
        ],
        ''
      );
      expect(html.indexOf('/blog/two')).to.be.lessThan(
        html.indexOf('/blog/one')
      );
      expect(html).to.contain('/blog/feed.xml');
    });

    it('renders an empty state rather than failing with no posts', () => {
      const html = render_blog_index_html(config, [], '');
      expect(html).to.contain('No posts yet.');
    });
  });

  describe('internal link labels', () => {
    it('labels /vs/ pages', () => {
      expect(get_internal_link_label('/vs/aws-agentcore')).to.equal(
        'Preloop vs Aws Agentcore'
      );
    });

    it('labels known static routes', () => {
      expect(get_internal_link_label('/whatis-mcp')).to.equal('What is MCP?');
      expect(get_internal_link_label('/pricing')).to.equal('Pricing');
    });
  });

  describe('feed', () => {
    it('emits one item per published post with a self link', () => {
      const xml = generate_blog_feed_xml(config, [
        makePost({ slug: 'one' }),
        makePost({ slug: 'draft', draft: true }),
      ]);
      expect(xml).to.contain('<rss version="2.0"');
      expect(xml).to.contain('https://preloop.ai/blog/one');
      expect(xml).to.not.contain('/blog/draft');
      expect(xml).to.contain('rel="self"');
    });

    it('escapes XML-hostile characters in titles', () => {
      const xml = generate_blog_feed_xml(config, [
        makePost({ title: 'Tokens & <schemas>' }),
      ]);
      expect(xml).to.contain('Tokens &amp; &lt;schemas&gt;');
    });
  });

  describe('llms.txt section', () => {
    it('lists posts with title, date and summary', () => {
      const lines = generate_blog_llms_section(config, [makePost()]);
      const joined = lines.join('\n');
      expect(joined).to.contain('A Post (2026-07-18)');
      expect(joined).to.contain('https://preloop.ai/blog/a-post');
      expect(joined).to.contain('A description.');
      expect(joined).to.contain('RSS feed');
    });

    it('is empty on self-hosted builds', () => {
      expect(
        generate_blog_llms_section(selfhosted, [makePost()])
      ).to.deep.equal([]);
    });
  });

  describe('duplicate title stripping', () => {
    it('strips a leading markdown heading that matches the title', () => {
      const body = strip_leading_markdown_title(
        '# A Post\n\nHello.\n',
        'A Post'
      );
      expect(body).to.equal('Hello.\n');
    });

    it('leaves a different leading heading alone', () => {
      const source = '# Not the title\n\nHello.\n';
      expect(strip_leading_markdown_title(source, 'A Post')).to.equal(source);
    });

    it('strips a matching rendered h1', () => {
      expect(
        strip_duplicate_title_heading('<h1>A Post</h1>\n<p>Hi</p>', 'A Post')
      ).to.equal('<p>Hi</p>');
    });

    it('treats nested markup in an h1 as the same title', () => {
      expect(
        strip_duplicate_title_heading(
          '<h1>A <em>Post</em></h1>\n<p>Hi</p>',
          'A Post'
        )
      ).to.equal('<p>Hi</p>');
    });
  });

  describe('constants', () => {
    it('uses /blog as the base path', () => {
      expect(BLOG_BASE_PATH).to.equal('/blog');
    });
  });
});
